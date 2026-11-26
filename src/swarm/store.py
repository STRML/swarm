"""Everything swarm keeps in Redis.

A room is one stream of posts plus a few keys per member. This module names those keys, validates what goes into
them, decides who receives a post, and manages the roster and the room's lifecycle.
"""
import argparse
import json
import os
import re
import sys
import time
from datetime import datetime, timezone

import redis

# Used when neither a flag nor the environment says otherwise.
DEFAULT_URL = "redis://127.0.0.1:6379/0"
DEFAULT_ROOM = "main"
DEFAULT_PREFIX = "swarm"

# Seconds the client waits on the socket before it declares the server unreachable.
SOCKET_TIMEOUT = 5
# Longest single blocking read, in ms. It has to end well inside SOCKET_TIMEOUT.
BLOCK_CHUNK_MS = 1000
# Seconds a member still counts as present after its last sign of life.
PRESENCE_TTL = 90
# Number of newest entries searched for recent posts on one topic.
TOPIC_SCAN_CAP = 1000

# A post with this body tells its recipients to stop.
SHUTDOWN = "__SHUTDOWN__"
# Exit statuses of a read: posts delivered, nothing for this agent, told to stop.
RC_DELIVERED = 0
RC_NONE = 10
RC_SHUTDOWN = 11

# Kinds reserved for `swarm worker`: "auto" when a seat ended without posting, "echo" when replying to auto posts.
WORKER_KINDS = ("auto", "echo")
MEMBER_FIELDS = ("role", "harness", "model", "workspace", "conversation")

MAX_RETAIN_DAYS = 3650  # ten years; keeps EXPIRE inside Redis's range
MAX_IDLE_HOURS = 87600  # ten years

_ID_RE = re.compile(r"^[A-Za-z0-9._-]+$")
_RECIPIENT_RE = re.compile(r"^@?[A-Za-z0-9._-]+$")
ARCHIVE_ID_RE = re.compile(r"^[A-Za-z0-9._-]+:[0-9]+$")


# ---- settings from the environment ----------------------------------------

def default_url():
    return os.environ.get("SWARM_REDIS_URL") or DEFAULT_URL


def default_room():
    return os.environ.get("SWARM_ROOM") or DEFAULT_ROOM


def key_prefix():
    raw = os.environ.get("SWARM_KEY_PREFIX") or DEFAULT_PREFIX
    if not _ID_RE.match(raw):
        sys.stderr.write(f"swarm: SWARM_KEY_PREFIX={raw!r} must match [A-Za-z0-9._-]+\n")
        raise SystemExit(2)
    return raw


def state_root():
    if os.environ.get("SWARM_STATE_DIR"):
        return os.path.abspath(os.environ["SWARM_STATE_DIR"])
    xdg = os.environ.get("XDG_STATE_HOME") or os.path.expanduser("~/.local/state")
    return os.path.join(xdg, "swarm")


# ---- key layout ------------------------------------------------------------

def _key(kind, *parts, prefix=None):
    """`<prefix>:<kind>:<part>...`. Parts are joined untouched, so '' and '*' work for slicing and scanning."""
    return ":".join(map(str, (prefix or key_prefix(), kind, *parts)))


def k_stream(room, prefix=None):
    return _key("stream", room, prefix=prefix)


def k_cursor(room, agent, prefix=None):
    return _key("cursor", room, agent, prefix=prefix)


def k_presence(room, agent, prefix=None):
    return _key("presence", room, agent, prefix=prefix)


def k_member(room, agent, prefix=None):
    return _key("member", room, agent, prefix=prefix)


def k_groups(room, agent, prefix=None):
    return _key("groups", room, agent, prefix=prefix)


def k_meta(room, prefix=None):
    return _key("meta", room, prefix=prefix)


def k_spawn_lock(room, prefix=None):
    return _key("lock", "spawn", room, prefix=prefix)


def archive_prefix(prefix=None):
    """Leading text of every archived key; the archive id and the original key follow it."""
    return _key("archived", "", prefix=prefix)


# ---- server and presence ---------------------------------------------------

def connect(url=None):
    """Return a text-mode client that has already answered a ping, so a dead server fails right here."""
    client = redis.Redis.from_url(
        url or default_url(),
        socket_timeout=SOCKET_TIMEOUT,
        decode_responses=True,
    )
    client.ping()
    return client


def now_iso():
    """The current UTC time to the second, as ISO 8601 with an explicit offset."""
    return datetime.now(tz=timezone.utc).isoformat(timespec="seconds")


def touch_presence(r, room, agent):
    """Count `agent` as present for another PRESENCE_TTL seconds. A blank agent is ignored."""
    if not agent:
        return
    r.set(name=k_presence(room, agent), value=now_iso(), ex=PRESENCE_TTL)


# ---- input validation ------------------------------------------------------

def _require(value, pattern, complaint):
    if pattern.match(value):
        return value
    raise argparse.ArgumentTypeError(f"'{value}'{complaint}")


def ident(value):
    """argparse type for a single name: room, agent, group, topic or post id."""
    return _require(value, _ID_RE, " is not a valid name; use only letters, digits, '.', '_' and '-'")


def archive_id(value):
    return _require(value, ARCHIVE_ID_RE, " must look like <room>:<epoch-ms>, as `swarm rooms --archived` prints it")


def recipients(value):
    """argparse type for `--to`. Repeats collapse in first-seen order, and `all` absorbs every other name."""
    names = dict.fromkeys(value.split(","))
    if not all(map(_RECIPIENT_RE.match, names)):
        raise argparse.ArgumentTypeError(
            f"'{value}': each recipient must be an id, @name or 'all' ([A-Za-z0-9._-]+)"
        )
    return "all" if "all" in names else ",".join(names)


def positive_int(value):
    """argparse type for a count of one or more, written in plain digits."""
    number = int(value) if value.isdigit() else 0
    if number > 0:
        return number
    raise argparse.ArgumentTypeError(f"expected a whole number above zero, got '{value}'")


def bounded_number(text, maximum):
    """`text` as a float within [0, maximum]; None for anything else, including nan and infinity."""
    try:
        number = float(text)
    except ValueError:
        return None
    if 0 <= number <= maximum:
        return number
    return None


def idle_hours(value):
    hours = bounded_number(value, MAX_IDLE_HOURS)
    if hours is not None:
        return hours
    raise argparse.ArgumentTypeError(f"'{value}' must be a number from 0 to {MAX_IDLE_HOURS}")


def env_number(name, default, maximum):
    """The bounded number in environment variable `name`, or `default` when unset or empty. Exits 2 if invalid."""
    raw = os.environ.get(name, "")
    if not raw:
        return default
    number = bounded_number(raw, maximum)
    if number is not None:
        return number
    sys.stderr.write(f"swarm: {name}={raw!r} must be a number from 0 to {maximum}\n")
    raise SystemExit(2)


def archive_retain_seconds():
    days = env_number("SWARM_ARCHIVE_RETAIN_DAYS", 7, MAX_RETAIN_DAYS)
    if days <= 0:
        sys.stderr.write("swarm: SWARM_ARCHIVE_RETAIN_DAYS must be greater than 0\n")
        raise SystemExit(2)
    return max(int(days * 86400), 1)


def archive_idle_hours():
    return env_number("SWARM_ARCHIVE_IDLE_HOURS", 6, MAX_IDLE_HOURS)


# ---- post shape ------------------------------------------------------------

# Stored fields in the order they are written, each with the value assumed when an entry lacks it.
_FIELD_DEFAULTS = {"from": "?", "to": "all", "topic": "", "reply_to": "", "kind": "msg", "body": ""}


def make_fields(frm, to, body, kind="msg", topic="", reply_to=""):
    """The field mapping to append to a room's stream."""
    return dict(zip(_FIELD_DEFAULTS, (frm, to, topic, reply_to, kind, body)))


def fields_to_msg(msg_id, fields):
    """A stored entry as a complete post dict, ignoring fields this version does not know."""
    known = {name: fields.get(name, default) for name, default in _FIELD_DEFAULTS.items()}
    return {"id": msg_id, **known}


def id_key(sid):
    """Sort key ordering stream ids by number, so '9-0' precedes '10-0'. A missing sequence counts as 0."""
    return ms_of(sid), int(sid.partition("-")[2] or 0)


def _is_shutdown(msg):
    return msg["body"].strip() == SHUTDOWN


def read_result_code(delivered):
    """Exit status for a batch of delivered posts. A shutdown anywhere in the batch wins."""
    if any(map(_is_shutdown, delivered)):
        return RC_SHUTDOWN
    return RC_DELIVERED if delivered else RC_NONE


# ---- display ---------------------------------------------------------------

# C0, DEL and C1 code points, each spelled out as \xNN. Newline and tab are ordinary text.
_CONTROL_ESCAPES = {
    code: f"\\x{code:02x}"
    for code in (*range(0x20), *range(0x7F, 0xA0))
    if chr(code) not in "\n\t"
}
_ONE_LINE_ESCAPES = {**_CONTROL_ESCAPES, ord("\n"): " "}


def terminal_text(value):
    """Untrusted text made safe to print, keeping its line breaks and tabs."""
    return str(value).translate(_CONTROL_ESCAPES)


def terminal_line(value):
    """Like terminal_text, with line breaks flattened to spaces."""
    return str(value).translate(_ONE_LINE_ESCAPES)


def _headline(msg):
    parts = [f"{terminal_line(msg['id'])}  {terminal_line(msg['from'])}"]
    audience = terminal_line(msg.get("to", "all"))
    if audience != "all":
        parts.append(f" -> {audience}")
    if msg.get("topic"):
        parts.append(f" [{terminal_line(msg['topic'])}]")
    if msg.get("reply_to"):
        parts.append(f" (re {terminal_line(msg['reply_to'])})")
    return "".join(parts)


def emit(msg, as_json):
    """Print one post: raw as a JSON line, or an escaped headline with the body indented beneath it."""
    if as_json:
        rendered = json.dumps(msg, ensure_ascii=False)
    else:
        rendered = f"{_headline(msg)}\n  {terminal_text(msg['body'])}"
    print(rendered)


# ---- delivery --------------------------------------------------------------


def addressed_to(to, agent, groups=()):
    """`to` is a comma list of ids, `@name`s and `all`; `@name` matches the agent or any member of group `name`."""
    targets = to.split(",")
    if "all" in targets or agent in targets:
        return True
    mentions = {t[1:] for t in targets if t.startswith("@")}
    return agent in mentions or bool(mentions & set(groups))


def is_stop(msg):
    return msg["body"].strip() == SHUTDOWN or msg.get("kind") == "done"


def ends_swarm(msg, sender_role):
    """The one rule for 'this post ends the swarm', shared by worker and board: __SHUTDOWN__ from anyone,
    or `--kind done` from a member whose roster role is coordinator."""
    if msg["body"].strip() == SHUTDOWN:
        return True
    return msg.get("kind") == "done" and sender_role == "coordinator"


def deliverable(msg, agent, topic=None, reply_to=None, groups=(), replay_floor=None):
    """Whether `agent` should receive this post.

    A post must be addressed to the agent. Its own posts are withheld, and `topic` and `reply_to` each demand an
    exact match; a shutdown overrides all three of those rules. A --replay join records `replay_floor`: stop
    signals up to and including that id ended an earlier swarm and are never delivered.
    """
    if not addressed_to(msg["to"], agent, groups):
        return False
    if replay_floor and is_stop(msg) and id_key(msg["id"]) <= id_key(replay_floor):
        return False
    if _is_shutdown(msg):
        return True
    if msg["from"] == agent:
        return False
    filters = (("topic", topic), ("reply_to", reply_to))
    return all(msg[field] == wanted for field, wanted in filters if wanted)


def read_from_cursor(r, room, agent, block_ms=None, count=100, topic=None, reply_to=None):
    """Read one batch of posts beyond the agent's cursor, leaving the cursor where it is.

    Returns (posts to deliver, id of the last entry examined or None). The caller saves that id as the new cursor
    once it has handled the batch. `block_ms` of None returns immediately.
    """
    cursor = r.get(k_cursor(room, agent)) or "0-0"
    groups = r.smembers(k_groups(room, agent))
    floor = r.hget(k_member(room, agent), "replay_floor")
    reply = r.xread({k_stream(room): cursor}, count=count, block=block_ms)
    if not reply:
        return [], None
    [(_, entries)] = reply
    posts = (fields_to_msg(*entry) for entry in entries)
    delivered = [
        post
        for post in posts
        if deliverable(post, agent, topic=topic, reply_to=reply_to, groups=groups, replay_floor=floor)
    ]
    last_seen = entries[-1][0] if entries else None
    return delivered, last_seen


def emit_recent(r, room, n, as_json, topic=None):
    """Print the latest `n` posts oldest first, optionally only those on `topic`.

    Returns the newest stream id examined, matching or not, so a follower can resume after it; None when the room
    is empty. A topic search looks no further back than TOPIC_SCAN_CAP entries.
    """
    window = r.xrevrange(k_stream(room), count=TOPIC_SCAN_CAP if topic else n)
    chosen = window
    if topic:
        chosen = [(msg_id, fields) for msg_id, fields in window if fields.get("topic") == topic][:n]
    for entry in reversed(chosen):
        emit(fields_to_msg(*entry), as_json)
    return window[0][0] if window else None


def room_names(r, room):
    """Every agent id and group name the room knows about."""
    names = set()
    for pattern in (k_member(room, "*"), k_presence(room, "*"), k_groups(room, "*")):
        for key in r.scan_iter(match=pattern):
            names.add(key.rsplit(":", 1)[-1])
    for key in r.scan_iter(match=k_groups(room, "*")):
        names |= r.smembers(key)
    return names


def unknown_mentions(r, room, to):
    mentions = [t[1:] for t in to.split(",") if t.startswith("@")]
    if not mentions:
        return []
    known = room_names(r, room)
    return [m for m in mentions if m not in known]


def join_member(r, room, agent, *, replay=False, groups=None, role=None, harness=None,
                model=None, workspace=None, conversation=None):
    """Register presence, set cursor ('0-0' on replay, else stream head), and store member metadata."""
    head_id = "0-0"
    try:
        last = r.xrevrange(k_stream(room), count=1)
        if last:
            head_id = last[0][0]
    except redis.ResponseError:
        pass
    cursor = "0-0" if replay else head_id
    r.set(k_cursor(room, agent), cursor)
    touch_presence(r, room, agent)
    meta = {
        "role": role,
        "harness": harness,
        "model": model,
        "workspace": workspace,
        "conversation": conversation,
    }
    fields = {k: v for k, v in meta.items() if v}
    fields["joined"] = now_iso()
    if replay:
        fields["replay_floor"] = head_id
    r.hset(k_member(room, agent), mapping=fields)
    if groups:
        r.sadd(k_groups(room, agent), *groups)
    return cursor


def roster_entry(r, room, agent):
    member = r.hgetall(k_member(room, agent))
    member.pop("replay_floor", None)
    return {
        "agent": agent,
        **member,
        "groups": sorted(r.smembers(k_groups(room, agent))),
        "present": bool(r.exists(k_presence(room, agent))),
    }


def list_roster(r, room):
    agents = set()
    for pattern in (k_member(room, "*"), k_presence(room, "*")):
        agents |= {key.rsplit(":", 1)[-1] for key in r.scan_iter(match=pattern)}
    return [roster_entry(r, room, a) for a in sorted(agents)]


# ---- trimming --------------------------------------------------------------

def xtrim_minid(r, stream, minid):
    """Drop exactly the entries older than `minid` and return how many went."""
    return r.xtrim(stream, minid=minid, approximate=False)


def cursor_has_unread_before(r, stream, cursor, boundary):
    """Whether any entry lies after `cursor` and before `boundary`, both ends excluded."""
    unread = r.xrange(stream, min=f"({cursor}", max=f"({boundary}", count=1)
    return len(unread) > 0


# ---- atomic room lifecycle (Lua) -------------------------------------------

# Every script runs atomically so a concurrent send, join or sweep lands wholly before or wholly after it.
# SCAN patterns are narrowed to exact key kinds so room `foo` never touches `foo-2`.
_LUA_COMMON = """
local function scan(pattern, keep)
  local found, seen, cursor = {}, {}, "0"
  repeat
    local page = redis.call("SCAN", cursor, "MATCH", pattern, "COUNT", 1000)
    cursor = page[1]
    for _, key in ipairs(page[2]) do
      if not seen[key] and keep(key) then seen[key] = true; found[#found + 1] = key end
    end
  until cursor == "0"
  return found
end
local function agent_keys(p, room, kinds)
  local prefixes = {}
  for _, kind in ipairs(kinds) do prefixes[#prefixes + 1] = p .. ":" .. kind .. ":" .. room .. ":" end
  return scan(p .. ":*:" .. room .. ":*", function(key)
    for _, pref in ipairs(prefixes) do
      if key:sub(1, #pref) == pref then return true end
    end
    return false
  end)
end
local function now_ms()
  local t = redis.call("TIME")
  return t[1] * 1000 + math.floor(t[2] / 1000)
end
"""

# ARGV: prefix, room, retain seconds, force (0/1), idle ms (empty = any age)
_LUA_ARCHIVE = _LUA_COMMON + """
local p, room, retain, force, idle = ARGV[1], ARGV[2], tonumber(ARGV[3]), ARGV[4] == "1", tonumber(ARGV[5])
local stream = p .. ":stream:" .. room
if redis.call("EXISTS", stream) == 0 then return {"absent"} end
local present = #agent_keys(p, room, {"presence"})
if present > 0 and not force then return {"present", present} end
local now = now_ms()
if idle then
  local last = redis.call("XREVRANGE", stream, "+", "-", "COUNT", 1)
  if #last > 0 and now - tonumber(last[1][1]:match("^(%d+)-")) < idle then return {"active"} end
end
local arch_root = p .. ":archived:"
local archive_id = room .. ":" .. string.format("%.0f", now)
while redis.call("EXISTS", arch_root .. archive_id .. ":" .. stream) == 1 do
  now = now + 1
  archive_id = room .. ":" .. string.format("%.0f", now)
end
local aprefix = arch_root .. archive_id .. ":"
local keys = agent_keys(p, room, {"member", "groups"})
keys[#keys + 1] = stream
local meta = p .. ":meta:" .. room
if redis.call("EXISTS", meta) == 1 then keys[#keys + 1] = meta end
for _, key in ipairs(keys) do
  redis.call("RENAME", key, aprefix .. key)
  redis.call("EXPIRE", aprefix .. key, retain)
end
if present > 0 then
  for _, key in ipairs(agent_keys(p, room, {"presence"})) do
    redis.call("UNLINK", key)
  end
end
return {"ok", archive_id, #keys}
"""

# ARGV: prefix, archive id
_LUA_UNARCHIVE = _LUA_COMMON + """
local p, aid = ARGV[1], ARGV[2]
local aprefix = p .. ":archived:" .. aid .. ":"
local keys = scan(aprefix .. "*", function() return true end)
if #keys == 0 then return {"absent"} end
for _, key in ipairs(keys) do
  local target = key:sub(#aprefix + 1)
  if redis.call("EXISTS", target) == 1 then return {"taken", target} end
end
for _, key in ipairs(keys) do
  local target = key:sub(#aprefix + 1)
  redis.call("RENAME", key, target)
  redis.call("PERSIST", target)
end
return {"ok", #keys}
"""

# ARGV: prefix, room, force (0/1)
_LUA_CLEAR = _LUA_COMMON + """
local p, room, force = ARGV[1], ARGV[2], ARGV[3] == "1"
local present = #agent_keys(p, room, {"presence"})
if present > 0 and not force then return {"present", present} end
local keys = agent_keys(p, room, {"member", "groups", "cursor", "presence"})
keys[#keys + 1] = p .. ":stream:" .. room
keys[#keys + 1] = p .. ":meta:" .. room
local removed = 0
for _, key in ipairs(keys) do removed = removed + redis.call("UNLINK", key) end
if removed == 0 then return {"absent"} end
return {"ok", removed}
"""


def archive_room(r, room, retain_seconds, force=False, idle_ms=None, prefix=None):
    p = prefix or key_prefix()
    status, *rest = r.eval(
        _LUA_ARCHIVE, 0, p, room, retain_seconds, int(force), "" if idle_ms is None else int(idle_ms)
    )
    if status == "present":
        return {"status": status, "present": rest[0]}
    if status == "ok":
        return {"status": status, "archive_id": rest[0], "keys": rest[1], "room": room}
    return {"status": status}


def unarchive_room(r, aid, prefix=None):
    p = prefix or key_prefix()
    status, *rest = r.eval(_LUA_UNARCHIVE, 0, p, aid)
    if status == "taken":
        return {"status": status, "key": rest[0]}
    if status == "ok":
        return {"status": status, "room": aid.rsplit(":", 1)[0], "keys": rest[0]}
    return {"status": status}


def clear_room(r, room, force=False, prefix=None):
    p = prefix or key_prefix()
    status, *rest = r.eval(_LUA_CLEAR, 0, p, room, int(force))
    if status == "present":
        return {"status": status, "present": rest[0]}
    return {"status": status, "keys": rest[0] if rest else 0}


def present_counts(r, prefix=None):
    p = prefix or key_prefix()
    counts = {}
    for key in r.scan_iter(match=f"{p}:presence:*:*"):
        room = key.split(":")[2]
        counts[room] = counts.get(room, 0) + 1
    return counts


def ms_of(stream_id):
    return int(stream_id.split("-")[0])


def iso_from_ms(ms):
    return datetime.fromtimestamp(ms / 1000, timezone.utc).isoformat(timespec="seconds") if ms else ""


def list_rooms(r, prefix=None):
    """Every room with a stream or live presence, newest activity first (title, messages, last_activity, present)."""
    p = prefix or key_prefix()
    stream_prefix = k_stream("", p)
    present = present_counts(r, p)
    stream_names = {
        n for n in (key[len(stream_prefix):] for key in r.scan_iter(match=k_stream("*", p)))
        if _ID_RE.match(n)
    }
    names = sorted(stream_names | {n for n in present if _ID_RE.match(n)})
    pipe = r.pipeline()
    for name in names:
        pipe.hget(k_meta(name, p), "title")
        pipe.xlen(k_stream(name, p))
        pipe.xrevrange(k_stream(name, p), count=1)
    results = pipe.execute()
    rows = []
    for i, name in enumerate(names):
        title, length, last = results[3 * i:3 * i + 3]
        last_ms = ms_of(last[0][0]) if last else None
        rows.append({
            "room": name,
            "title": title or "",
            "messages": length,
            "last_activity_ms": last_ms,
            "last_activity": iso_from_ms(last_ms),
            "present": present.get(name, 0),
        })
    return sorted(rows, key=lambda row: (row["last_activity_ms"] or 0, row["present"]), reverse=True)


def list_archived(r, prefix=None):
    """Archived rooms, newest archive first, with remaining retention."""
    p = prefix or key_prefix()
    aprefix = archive_prefix(p)
    rows = []
    for key in r.scan_iter(match=f"{aprefix}*:{p}:stream:*"):
        room, stamp, _ = key[len(aprefix):].split(":", 2)
        aid = f"{room}:{stamp}"
        ttl = max(r.ttl(key), 0)
        rows.append({
            "archive_id": aid,
            "room": room,
            "archived_ms": int(stamp),
            "archived": iso_from_ms(int(stamp)),
            "title": r.hget(f"{aprefix}{aid}:{k_meta(room, p)}", "title") or "",
            "messages": r.xlen(key),
            "expires_in": ttl,
            "expires": iso_from_ms(int(time.time() * 1000) + ttl * 1000),
        })
    return sorted(rows, key=lambda row: row["archived_ms"], reverse=True)


def sweep_idle(r, hours, retain_seconds, archived=None, prefix=None):
    """Archive every idle room with nobody present; returns archive results. 0 hours disables."""
    archived = [] if archived is None else archived
    if not hours:
        return archived
    idle_ms = hours * 3_600_000
    cutoff = time.time() * 1000 - idle_ms
    for row in list_rooms(r, prefix=prefix):
        if row["present"] or (row["last_activity_ms"] or 0) > cutoff:
            continue
        result = archive_room(r, row["room"], retain_seconds, idle_ms=idle_ms, prefix=prefix)
        if result["status"] == "ok":
            archived.append(result)
    return archived
