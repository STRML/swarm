"""Unified `swarm` CLI: durable agent rooms, multi-harness swarm orchestration, and live TUI board."""
import argparse
import codecs
import json
import os
import shutil
import sys
import time
from collections import defaultdict, deque

from swarm import __version__, store
from swarm import config as swarm_config

DONE_SUMMARY = "Swarm finished."
FOLLOW_POLL_SECONDS = 0.1


def _post(r, room, frm, to, body, **fields):
    receipt = r.xadd(store.k_stream(room), store.make_fields(frm, to, body, **fields))
    store.touch_presence(r, room, frm)
    return receipt


def _read_body(body):
    """The stripped message body: stdin for '-' or a pipe with no argument, else the argument."""
    use_stdin = body == "-" or (body is None and not sys.stdin.isatty())
    return (sys.stdin.read() if use_stdin else body or "").strip()


def cmd_send(r, args):
    body = _read_body(args.body)
    if not body:
        print("swarm send: empty body", file=sys.stderr)
        return 2
    if args.kind in store.WORKER_KINDS:
        print(f"swarm send: --kind {args.kind} is reserved for swarm worker's own posts", file=sys.stderr)
        return 2
    receipt = _post(
        r,
        args.room,
        args.frm,
        args.to,
        body,
        kind=args.kind,
        topic=args.topic or "",
        reply_to=args.reply_to or "",
    )
    for mention in store.unknown_mentions(r, args.room, args.to):
        print(
            f"swarm send: warning: no agent or group '@{mention}' in room={args.room} yet; "
            "it will be delivered if one joins with --replay",
            file=sys.stderr,
        )
    confirmation = (
        json.dumps({"id": receipt, "room": args.room})
        if args.json else f"sent {receipt} to room={args.room} to={args.to}"
    )
    print(confirmation)
    return 0


def cmd_done(r, args):
    body = _read_body(args.body)
    if not body and args.body is not None:
        print("swarm done: empty body", file=sys.stderr)
        return 2
    if r.hget(store.k_member(args.room, args.frm), "role") != "coordinator":
        print(
            f"swarm done: warning: '{args.frm}' does not have the coordinator role in room={args.room}, "
            "so workers will not retire; pass --from <coordinator id>",
            file=sys.stderr,
        )
    receipt = _post(r, args.room, args.frm, "all", body or DONE_SUMMARY, kind="done")
    print(
        json.dumps({"id": receipt, "room": args.room, "kind": "done"})
        if args.json else f"sent {receipt} to room={args.room} to=all (done)"
    )
    return 0


def _write_stop_file(room, agent, reason):
    path = store.room_file(room, f"stop-{agent}")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as sentinel:
        sentinel.write(f"{reason}\n" if reason else "")


def cmd_stop(r, args):
    room, agent = args.room, args.agent
    if agent == "all":
        print("swarm stop: 'all' would retire every worker; use `swarm done` to end the swarm", file=sys.stderr)
        return 2
    if not r.exists(store.k_member(room, agent), store.k_presence(room, agent)):
        print(f"swarm stop: warning: no agent '{agent}' in room={room}", file=sys.stderr)
    receipt = _post(r, room, args.frm, agent, store.SHUTDOWN)
    try:
        _write_stop_file(room, agent, args.reason)
    except OSError as exc:
        print(f"swarm stop: the shutdown was sent ({receipt}) but the stop file failed: {exc}", file=sys.stderr)
        return 1
    print(
        json.dumps({"stopped": agent, "room": room, "id": receipt})
        if args.json else f"stopped {agent} in room={room} (sent {receipt})"
    )
    return 0


def _read_and_advance(r, args, *, block_ms=None, reply_to=None):
    messages, examined_through = store.read_from_cursor(
        r, args.room, args.as_agent, topic=args.topic, block_ms=block_ms, reply_to=reply_to,
    )
    if examined_through:
        r.set(store.k_cursor(args.room, args.as_agent), examined_through)
    return messages


def _report_delivery(messages, as_json, empty_text):
    if not messages and not as_json:
        print(empty_text)
    for message in messages:
        store.emit(message, as_json)
    return store.read_result_code(messages)


def cmd_poll(r, args):
    store.touch_presence(r, args.room, args.as_agent)
    return _report_delivery(_read_and_advance(r, args), args.json, "(no new messages)")


def cmd_wait(r, args):
    store.touch_presence(r, args.room, args.as_agent)
    deadline = time.monotonic() + args.timeout
    messages = []
    while not messages:
        seconds_left = deadline - time.monotonic()
        if seconds_left <= 0:
            break
        chunk_ms = min(store.BLOCK_CHUNK_MS, max(1, int(seconds_left * 1000)))
        messages = _read_and_advance(r, args, block_ms=chunk_ms, reply_to=args.reply_to)
    return _report_delivery(messages, args.json, "(timeout, no messages)")


def cmd_tail(r, args):
    store.emit_recent(r, room=args.room, n=args.n, as_json=args.json)
    return 0


def cmd_history(r, args):
    stream_key = store.k_stream(args.room)
    for entry in r.xrange(stream_key, count=args.n):
        store.emit(store.fields_to_msg(*entry), args.json)
    return 0


def cmd_watch(r, args):
    stream_key = store.k_stream(args.room)
    start = store.emit_recent(r, args.room, args.n, args.json, topic=args.topic) if args.n > 0 else None
    offset = start or "$"
    sys.stdout.flush()
    try:
        while True:
            for _, batch in r.xread({stream_key: offset}, block=store.BLOCK_CHUNK_MS):
                for offset, fields in batch:
                    if not args.topic or fields.get("topic") == args.topic:
                        store.emit(store.fields_to_msg(offset, fields), args.json)
                sys.stdout.flush()
    except KeyboardInterrupt:
        return 0


def cmd_thread(r, args):
    messages = {
        entry_id: store.fields_to_msg(entry_id, fields)
        for entry_id, fields in r.xrange(store.k_stream(args.room))
    }
    if args.id not in messages:
        print(f"swarm thread: message {args.id} not in room {args.room}", file=sys.stderr)
        return 1

    links = defaultdict(list)
    for entry_id, message in messages.items():
        parent = message["reply_to"]
        if parent in messages:
            links[entry_id].append(parent)
            links[parent].append(entry_id)

    thread = {args.id}
    pending = [args.id]
    while pending:
        current = pending.pop()
        unseen = [neighbor for neighbor in links.get(current, ()) if neighbor not in thread]
        thread.update(unseen)
        pending.extend(unseen)
    for entry_id in sorted(thread, key=store.id_key):
        store.emit(messages[entry_id], args.json)
    return 0


def _inbox_line(message):
    marker = "Q" if message["kind"] == "question" else "@"
    entry_id = store.terminal_line(message["id"])
    sender = store.terminal_line(message["from"])
    topic = f" [{store.terminal_line(message['topic'])}]" if message["topic"] else ""
    excerpt = store.terminal_line(message["body"])[:100]
    return f"{marker} {entry_id}  {sender}{topic}: {excerpt}"


def cmd_inbox(r, args):
    room, agent = args.room, args.as_agent
    store.touch_presence(r, room, agent)
    offset = r.get(store.k_cursor(room, agent)) or "0-0"
    limit = 1000
    streams = r.xread({store.k_stream(room): offset}, count=limit)
    entries = streams[0][1] if streams else ()
    groups = r.smembers(store.k_groups(room, agent))

    visible = []
    for entry in entries:
        message = store.fields_to_msg(*entry)
        if message["from"] == agent or (message["to"] == "all" and message["kind"] != "question"):
            continue
        if store.addressed_to(message["to"], agent, groups):
            visible.append(message)

    if len(entries) == limit:
        print(f"swarm inbox: showing the oldest {limit} unread; more remain - poll to advance", file=sys.stderr)
    if not args.json and not visible:
        print("(inbox empty - no unread directed messages or questions)")
    for message in visible:
        if args.json:
            store.emit(message, True)
        else:
            print(_inbox_line(message))
    return 0


def _lagging_readers(r, room, stream, boundary):
    for cursor_key in r.scan_iter(match=store.k_cursor(room, "*")):
        offset = r.get(cursor_key)
        if offset and store.cursor_has_unread_before(r, stream, offset, boundary):
            agent = cursor_key.rsplit(":", 1)[-1]
            yield agent, offset


def cmd_prune(r, args):
    room, keep = args.room, args.keep
    stream = store.k_stream(room)
    before = r.xlen(stream)
    if before <= keep:
        print(f"room {room}: {before} messages <= keep {keep}; nothing to trim")
        return 0

    boundary, _ = r.xrevrange(stream, count=keep)[-1]
    lagging = list(_lagging_readers(r, room, stream, boundary))
    if lagging and not args.force:
        readers = ", ".join(
            f"{store.terminal_line(agent)}@{store.terminal_line(offset)}" for agent, offset in lagging
        )
        print(
            f"swarm prune: REFUSING - {len(lagging)} agent cursor(s) behind the keep-window "
            f"boundary {store.terminal_line(boundary)}; trimming would drop unread messages ({readers}). "
            "Re-run with --force to override.",
            file=sys.stderr,
        )
        return 1
    if lagging:
        print(f"swarm prune: --force: dropping unread for {len(lagging)} lagging agent(s)", file=sys.stderr)
    removed = store.xtrim_minid(r, stream, boundary)
    print(f"pruned room {room}: removed {removed}, kept {r.xlen(stream)} (was {before})")
    return 0


def cmd_join(r, args):
    room, agent = args.room, args.as_agent
    metadata = {field: getattr(args, field) for field in store.MEMBER_FIELDS}
    cursor = store.join_member(
        r, room, agent, replay=args.replay, groups=args.group, **metadata,
    )
    if args.json:
        print(json.dumps({"joined": agent, "room": room, "cursor": cursor}))
        return 0
    print(f"{agent} joined room={room} (cursor at {cursor})")
    return 0


def cmd_state(r, args):
    fields = {"state": args.state, "state_at": store.now_iso()}
    if args.doing is not None:
        fields["doing"] = args.doing
    r.hset(store.k_member(args.room, args.as_agent), mapping=fields)
    store.touch_presence(r, args.room, args.as_agent)
    return 0


def cmd_usage(r, args):
    key = store.k_member(args.room, args.as_agent)
    pipe = r.pipeline()
    pipe.hincrby(key, "tokens_in", args.tokens_in)
    pipe.hincrby(key, "tokens_cached", args.tokens_cached)
    pipe.hincrby(key, "tokens_out", args.tokens_out)
    pipe.hincrby(key, "turns", 1)
    if args.cost is not None:
        pipe.hincrbyfloat(key, "cost_usd", args.cost)
    pipe.execute()
    return 0


def cmd_title(r, args):
    if args.text:
        r.hset(store.k_meta(args.room), "title", args.text)
    print(r.hget(store.k_meta(args.room), "title") or args.room)
    return 0


def cmd_roster(r, args):
    roster = store.list_roster(r, args.room)
    if args.json:
        print(json.dumps(roster))
        return 0
    for m in roster:
        state = m.get("state", "?") if m["present"] else "gone"
        groups = ",".join("@" + g for g in m["groups"])
        print(
            f"{store.terminal_line(m['agent'])}\t{state}\t{store.terminal_line(m.get('role', ''))}\t"
            f"{store.terminal_line(m.get('model', ''))}\t{store.terminal_line(groups)}"
        )
    return 0


def cmd_agents(r, args):
    presence_keys = list(r.scan_iter(match=store.k_presence(args.room, "*")))
    timestamps = r.mget(presence_keys) if presence_keys else ()
    agents = [(key.rsplit(":", 1)[-1], stamp) for key, stamp in zip(presence_keys, timestamps)]
    agents.sort(key=lambda entry: entry[0])
    if args.json:
        print(json.dumps([{"agent": agent, "last_seen": stamp} for agent, stamp in agents]))
        return 0
    if not agents:
        print(f"(no agents present in room={args.room})")
    for agent, stamp in agents:
        print(f"{agent}\tlast_seen={stamp}")
    return 0


# ---- room lifecycle commands -----------------------------------------------

def report_swept(swept):
    for result in swept:
        sys.stderr.write(
            f"swarm: auto-archived idle room {store.terminal_line(result['room'])} as {result['archive_id']}\n"
        )


def cmd_rooms(r, args):
    if args.archived:
        rows = store.list_archived(r)
        columns = ("archive_id", "title", "messages", "archived", "expires")
    else:
        report_swept(store.sweep_idle(r, store.archive_idle_hours(), store.archive_retain_seconds()))
        rows = store.list_rooms(r)
        columns = ("room", "title", "messages", "last_activity", "present")
    if args.json:
        print(json.dumps(rows))
        return 0
    if not rows:
        print("no archived rooms" if args.archived else "no rooms")
        return 0
    print("\t".join(c.upper() for c in columns))
    for row in rows:
        print("\t".join(store.terminal_line(str(row[c]) or "-") for c in columns))
    return 0


def require_room(args, verb):
    if args.room_given:
        return True
    sys.stderr.write(f"swarm {verb}: --room is required (it never falls back to the default room)\n")
    return False


def refuse_present(verb, room, count):
    sys.stderr.write(
        f"swarm {verb}: REFUSING - {count} member(s) present in room {store.terminal_line(room)}. "
        "Re-run with --force to override.\n"
    )
    return 1


def cmd_archive(r, args):
    if args.idle_hours is not None:
        if args.room_given or args.force:
            sys.stderr.write(
                "swarm archive: --idle-hours sweeps every idle room; it takes no --room or --force "
                "(a room with a present member is never auto-archived)\n"
            )
            return 2
        swept = store.sweep_idle(r, args.idle_hours, store.archive_retain_seconds())
        report_swept(swept)
        print(json.dumps(swept) if args.json else f"archived {len(swept)} idle room(s)")
        return 0
    if not require_room(args, "archive"):
        return 2
    retain = store.archive_retain_seconds()
    result = store.archive_room(r, args.room, retain, force=args.force)
    if result["status"] == "present":
        return refuse_present("archive", args.room, result["present"])
    if result["status"] != "ok":
        sys.stderr.write(f"swarm archive: no room {store.terminal_line(args.room)}\n")
        return 1
    if args.json:
        print(json.dumps(result))
    else:
        print(
            f"archived room={args.room} as {result['archive_id']} "
            f"({result['keys']} keys, kept {retain // 86400}d; undo with: swarm unarchive {result['archive_id']})"
        )
    return 0


def cmd_unarchive(r, args):
    result = store.unarchive_room(r, args.archive_id)
    if result["status"] == "absent":
        sys.stderr.write(f"swarm unarchive: no archive {store.terminal_line(args.archive_id)} (expired or never existed)\n")
        return 1
    if result["status"] == "taken":
        sys.stderr.write(
            f"swarm unarchive: REFUSING - the live key {store.terminal_line(result['key'])} already exists; "
            "clear or archive the live room first.\n"
        )
        return 1
    print(json.dumps(result) if args.json else f"restored room={result['room']} ({result['keys']} keys)")
    return 0


def cmd_clear(r, args):
    if not require_room(args, "clear"):
        return 2
    result = store.clear_room(r, args.room, force=args.force)
    if result["status"] == "present":
        return refuse_present("clear", args.room, result["present"])
    if result["status"] != "ok":
        sys.stderr.write(f"swarm clear: no room {store.terminal_line(args.room)}\n")
        return 1
    print(json.dumps(result) if args.json else f"cleared room={args.room} ({result['keys']} keys deleted)")
    return 0


# ---- worker logs -----------------------------------------------------------

def _print_log(log, count, follow):
    decoder = codecs.getincrementaldecoder("utf-8")("replace")
    chunk = b"".join(deque(log, maxlen=count))
    while True:
        sys.stdout.write(store.terminal_text(decoder.decode(chunk, final=not follow)))
        sys.stdout.flush()
        if not follow:
            return
        chunk = log.read(65536)
        if not chunk:
            time.sleep(FOLLOW_POLL_SECONDS)


def cmd_logs(args):
    path = store.room_file(args.room, f"{args.agent}.log")
    try:
        with open(path, "rb") as log:
            _print_log(log, args.n, args.follow)
    except FileNotFoundError:
        print(f"swarm logs: no log for '{args.agent}' in room={args.room}: {path}", file=sys.stderr)
        return 1
    except OSError as exc:
        print(f"swarm logs: cannot read {path}: {exc.strerror}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        pass
    return 0


# ---- doctor ----------------------------------------------------------------

def seat_binary(seat_cfg):
    kind = seat_cfg["kind"]
    if kind == "claude":
        launcher = seat_cfg.get("launcher") or ["claude"]
        return launcher[-1] if launcher[0] == "env" else launcher[0]
    if kind in ("agy", "cmd"):
        cmd = seat_cfg.get("command") or ([kind] if kind == "agy" else [])
        return cmd[0] if cmd else None
    return kind


def cmd_doctor(args):
    report = {
        "redis": {"url": args.url, "ok": False},
        "config": {"path": swarm_config.config_path(), "exists": False, "ok": False},
        "seats": {},
        "state_dir": store.state_root(),
        "key_prefix": store.key_prefix(),
    }
    try:
        r = store.connect(args.url)
        info = r.info("server")
        report["redis"].update(ok=True, version=info.get("redis_version", "unknown"))
    except Exception as exc:  # noqa: BLE001
        report["redis"]["error"] = str(exc)

    cfg_path = swarm_config.config_path()
    report["config"]["exists"] = os.path.exists(cfg_path)
    try:
        cfg = swarm_config.load(cfg_path)
        report["config"]["ok"] = True
        for name, entry in sorted(cfg["seats"].items()):
            binary = seat_binary(entry)
            found = shutil.which(binary) if binary else None
            report["seats"][name] = {"kind": entry["kind"], "binary": binary, "found": bool(found), "path": found}
    except SystemExit as exc:
        report["config"]["error"] = str(exc)

    if args.json:
        print(json.dumps(report, indent=2))
    else:
        redis_line = f"ok (redis {report['redis'].get('version')})" if report["redis"]["ok"] else f"FAIL ({report['redis'].get('error')})"
        print(f"redis:   {redis_line}  [{args.url}]")
        cfg_note = "loaded" if report["config"]["exists"] else "built-in defaults"
        cfg_line = f"ok ({cfg_note})" if report["config"]["ok"] else f"FAIL ({report['config'].get('error')})"
        print(f"config:  {cfg_line}  [{cfg_path}]")
        print(f"prefix:  {report['key_prefix']}  state: {report['state_dir']}")
        if report["seats"]:
            print("seats:")
            for name, s in report["seats"].items():
                status = s["path"] if s["found"] else (f"not on PATH ({s['binary']})" if s["binary"] else "env SWARM_SEAT_CMD")
                print(f"  {name:<12} ({s['kind']:<6})  {status}")
    return 0 if report["redis"]["ok"] and report["config"]["ok"] else 1


# ---- argparse --------------------------------------------------------------

def build_parser():
    parser = argparse.ArgumentParser(
        prog="swarm",
        description="Durable Redis Streams rooms and multi-harness agent swarms.",
    )
    default_url = store.default_url()
    parser.add_argument("--url", default=default_url, help=f"Redis URL (default {default_url})")
    parser.add_argument(
        "--room", type=store.room_arg,
        help=f"room name, or . for this git repository's name (default {store.default_room()})",
    )
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    parser.add_argument("--version", action="version", version=f"swarm {__version__}")

    shared = argparse.ArgumentParser(add_help=False, argument_default=argparse.SUPPRESS)
    shared.add_argument("--url", help="Redis URL")
    shared.add_argument("--room", type=store.room_arg, help="room name, or . for this git repository's name")
    shared.add_argument("--json", action="store_true", help="machine-readable output")
    commands = parser.add_subparsers(dest="cmd")

    def command(name, help_text, *, agent=False, topic=False):
        options = commands.add_parser(name, parents=[shared], help=help_text)
        if agent:
            options.add_argument("--as", dest="as_agent", type=store.ident, required=True, help="your agent id")
        if topic:
            options.add_argument("--topic", type=store.ident, help="only deliver messages with this exact topic")
        return options

    options = command("send", "publish a message to a room")
    options.add_argument("--from", dest="frm", type=store.ident, required=True, help="your agent id")
    options.add_argument(
        "--to", type=store.recipients, default="all",
        help="comma list of agent ids, @name (agent or group) and 'all' (default all)",
    )
    options.add_argument("--topic", type=store.ident, help="optional topic slug ([A-Za-z0-9._-]+)")
    options.add_argument("--reply-to", default="", help="message id you're replying to")
    options.add_argument("--kind", default="msg", help="msg|question|answer|system|done (default msg)")
    options.add_argument("body", nargs="?", help="message text, or '-' / piped stdin")

    options = command("done", "end the swarm: post --kind done to all")
    options.add_argument(
        "--from", dest="frm", type=store.ident, default="coordinator", help="coordinator id (default coordinator)"
    )
    options.add_argument("body", nargs="?", help=f"summary, or '-' / piped stdin (default {DONE_SUMMARY!r})")

    options = command("stop", "retire one worker and leave the rest of the room running")
    options.add_argument("agent", type=store.ident, help="member id to stop")
    options.add_argument("reason", nargs="?", help="why, recorded in the worker's stop file")
    options.add_argument(
        "--from", dest="frm", type=store.ident, default="coordinator", help="sender id (default coordinator)"
    )

    options = command("logs", "show a worker's log file (no Redis needed)")
    options.add_argument("agent", type=store.ident, help="member id")
    options.add_argument("-n", type=store.non_negative_int, default=50, help="last N lines (default 50)")
    options.add_argument("-f", "--follow", action="store_true", help="keep printing as the log grows")

    command("poll", "read new messages addressed to you (non-blocking)", agent=True, topic=True)
    options = command("wait", "block until a message addressed to you arrives", agent=True, topic=True)
    options.add_argument("--timeout", type=float, default=30.0, help="seconds (default 30)")
    options.add_argument("--reply-to", type=store.ident, help="only wake on replies to this id")

    command("tail", "show recent messages without moving your cursor").add_argument("-n", type=int, default=20)
    command("history", "print the full room log").add_argument("-n", type=int, default=1000)
    options = command("watch", "live-follow all messages in a room (read-only observer)", topic=True)
    options.add_argument("-n", type=int, default=10, help="show last N for context before following (default 10)")

    options = command("thread", "print the full reply_to thread containing a message id")
    options.add_argument("id", type=store.ident, help="a message id somewhere in the thread")
    command("inbox", "peek unread directed messages or questions; cursor untouched", agent=True)

    options = command("prune", "trim a room to the newest --keep messages (destructive, guarded)")
    options.add_argument("--keep", type=store.positive_int, default=5000, help="messages to keep (default 5000)")
    options.add_argument("--force", action="store_true", help="trim even if it drops unread for lagging agents")

    options = command("rooms", "list live rooms (and sweep idle ones); --archived lists archives")
    options.add_argument("--archived", action="store_true", help="list archived rooms instead")
    options = command("archive", "archive a room (--room R) or sweep idle rooms (--idle-hours N)")
    options.add_argument(
        "--idle-hours", type=store.idle_hours,
        help="archive every room idle longer than N hours with nobody present (0 disables)",
    )
    options.add_argument("--force", action="store_true", help="archive even though members are present")
    options = command("unarchive", "restore an archived room (refuses if live name is taken)")
    options.add_argument("archive_id", type=store.archive_id, help="<room>:<epoch-ms> from `swarm rooms --archived`")
    options = command("clear", "delete a room and every key of it now (destructive, guarded)")
    options.add_argument("--force", action="store_true", help="clear even though members are present")

    options = command("join", "register presence and set cursor to now (or the start with --replay)", agent=True)
    options.add_argument("--replay", action="store_true", help="start cursor at the beginning of the room")
    options.add_argument("--group", action="append", type=store.ident, help="join group NAME (repeatable); reached as @NAME")
    for field in store.MEMBER_FIELDS:
        options.add_argument(f"--{field}", help=f"record this member's {field} for the roster")
    command("agents", "list agents currently present in a room")

    options = command("state", "set your roster state: working, waiting or retired", agent=True)
    options.add_argument("state", choices=("working", "waiting", "retired"))
    options.add_argument("--doing", help="one line on what you are doing now, shown on the board")
    options = command("usage", "add one turn's token usage to your roster totals", agent=True)
    options.add_argument("--tokens-in", type=int, default=0, help="fresh input tokens (not served from prompt cache)")
    options.add_argument("--tokens-cached", type=int, default=0, help="input tokens read from the prompt cache")
    options.add_argument("--tokens-out", type=int, default=0)
    options.add_argument("--cost", type=float, help="USD for this turn, when reported")
    command("title", "print or set the room title").add_argument("text", nargs="?")
    command("roster", "list members with role, model, state, groups and presence")

    options = command("spawn", "pick a seat, join a member, post its brief and start its worker")
    options.add_argument("--as", dest="agent", type=store.ident, required=True, help="member id")
    options.add_argument("--harness", help="seat name from config; omit to pick by role band and quota")
    options.add_argument("--effort", choices=("low", "medium", "high", "xhigh", "max"))
    options.add_argument("--model")
    options.add_argument("--role", default="worker")
    options.add_argument("--group", action="append", type=store.ident)
    options.add_argument("--workspace")
    options.add_argument("--brief", help="file with the member's brief, or - for stdin")
    options.add_argument("--from", dest="frm", type=store.ident, default="coordinator", help="who posts the brief")
    options.add_argument("--base", help="ref the member's work starts from; read-only seats see commits since it")

    options = command("worker", "run one headless swarm member loop (usually launched by `swarm spawn`)")
    options.add_argument("agent", type=store.ident)
    options.add_argument("harness", help="seat name from the swarm config")
    options.add_argument("--model")
    options.add_argument("--effort")
    options.add_argument("--workspace")

    command("board", "open the terminal TUI board (or room picker when --room is omitted)")
    command("doctor", "check Redis connectivity, config file and seat binaries")
    options = command("init", "write a starter config for the harnesses found on PATH")
    options.add_argument("--force", action="store_true", help="overwrite an existing config")
    options.add_argument(
        "--link-skills", action="store_true", help="symlink the bundled skills into your harness skills directories"
    )
    return parser


DISPATCH = {
    "send": cmd_send,
    "done": cmd_done,
    "stop": cmd_stop,
    "poll": cmd_poll,
    "wait": cmd_wait,
    "tail": cmd_tail,
    "history": cmd_history,
    "watch": cmd_watch,
    "thread": cmd_thread,
    "inbox": cmd_inbox,
    "prune": cmd_prune,
    "rooms": cmd_rooms,
    "archive": cmd_archive,
    "unarchive": cmd_unarchive,
    "clear": cmd_clear,
    "join": cmd_join,
    "agents": cmd_agents,
    "state": cmd_state,
    "usage": cmd_usage,
    "title": cmd_title,
    "roster": cmd_roster,
}


def main(argv=None):
    args = build_parser().parse_args(argv)
    args.room_given = args.room is not None
    args.room = args.room or store.resolve_room(store.default_room())

    if args.cmd is None:
        if not args.json and sys.stdin.isatty() and sys.stdout.isatty():
            args.cmd = "board"
        else:
            args.cmd = "rooms"
            args.archived = False

    if args.cmd == "doctor":
        return cmd_doctor(args)
    if args.cmd == "logs":
        return cmd_logs(args)
    if args.cmd == "init":
        from swarm import setup
        return setup.run_init(args)
    if args.cmd == "spawn":
        from swarm import spawn
        return spawn.run_spawn(args)
    if args.cmd == "worker":
        from swarm import worker
        return worker.run_worker(args)
    if args.cmd == "board":
        from swarm import board
        return board.run_board(args)

    try:
        r = store.connect(args.url)
    except Exception as exc:  # noqa: BLE001
        sys.stderr.write(f"swarm: cannot reach Redis at {args.url}: {exc}\n")
        sys.stderr.write("  run `swarm doctor` or start Redis locally\n")
        return 3
    return DISPATCH[args.cmd](r, args)


if __name__ == "__main__":
    sys.exit(main())
