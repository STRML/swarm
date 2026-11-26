"""Run one swarm member: sleep on Redis Streams, wake a seat (claude, codex, omp, agy, cmd) when addressed.

The member must already have joined the room (`swarm spawn` joins with `--replay`); the worker never re-joins,
so a brief posted before it started is still delivered. Each wake:
  state=working (heartbeat keeps presence alive) -> run seat with new posts on stdin ->
  record tokens/cost -> auto-post output if the seat didn't post -> state=waiting.
Exit 0 on __SHUTDOWN__, coordinator `--kind done`, or stop file; exit 3 when this id is already running.
"""
import importlib.resources
import json
import os
import shlex
import subprocess
import sys
import threading
import time
import uuid

from swarm import config as swarm_config
from swarm import store

AUTO = "auto"
ECHO = "echo"
SILENT_REPLY = "NO_REPLY"
RESUME_FAILED = ("no conversation found", "session not found", "could not resume", "no session")
AGY_NOTE = (
    "You cannot run tools or read files in this seat: do not call any tool. Everything you can see is in "
    "this prompt, including the workspace snapshot at the end. Your whole reply is posted to the board for you."
)
SNAPSHOT_LIMIT = 60_000
BASE_CANDIDATES = ("@{upstream}", "origin/HEAD", "origin/main", "origin/master", "main", "master")


def default_worker_prompt():
    try:
        return importlib.resources.files("swarm").joinpath("prompts", "worker.md").read_text(encoding="utf-8")
    except (FileNotFoundError, ModuleNotFoundError):
        return ""


class Worker:
    def __init__(self, args, cfg):
        self.agent = args.agent
        self.harness = args.harness
        self.seat = swarm_config.seat(cfg, args.harness)
        self.claude_flags = cfg["claude_flags"]
        self.model = args.model
        self.effort = args.effort
        self.workspace = args.workspace
        self.url = getattr(args, "url", None) or store.default_url()
        self.room = getattr(args, "room", None) or store.default_room()
        self.r = store.connect(self.url)
        self.state_dir = os.path.join(store.state_root(), self.room)
        self.coordinator = os.environ.get("SWARM_COORDINATOR", "coordinator")
        self.wait_secs = float(os.environ.get("SWARM_WAIT_SECS", "60"))
        self.heartbeat_secs = float(os.environ.get("SWARM_HEARTBEAT_SECS", "30"))
        self.seat_timeout = float(os.environ.get("SWARM_SEAT_TIMEOUT", "3600"))
        os.makedirs(self.state_dir, exist_ok=True)

    # ---- files: pid lock, session id, stop sentinels -------------------------

    def path(self, name):
        return os.path.join(self.state_dir, f"{self.agent}.{name}")

    def lock(self):
        pid_file = self.path("pid")
        try:
            other = int(open(pid_file).read().strip())
            os.kill(other, 0)
            sys.stderr.write(f"swarm worker: {self.agent} already running as pid {other}\n")
            sys.exit(3)
        except (FileNotFoundError, ValueError, ProcessLookupError, PermissionError):
            pass
        with open(pid_file, "w") as f:
            f.write(str(os.getpid()))

    def unlock(self):
        try:
            os.remove(self.path("pid"))
        except FileNotFoundError:
            pass

    def session(self):
        try:
            return open(self.path("session")).read().strip()
        except FileNotFoundError:
            return ""

    def save_session(self, sid):
        if sid:
            with open(self.path("session"), "w") as f:
                f.write(sid)

    def drop_session(self):
        try:
            os.remove(self.path("session"))
        except FileNotFoundError:
            pass

    def should_stop(self):
        return any(os.path.exists(os.path.join(self.state_dir, n)) for n in ("stop-all", f"stop-{self.agent}"))

    # ---- store ---------------------------------------------------------------

    def set_state(self, state, doing=None):
        fields = {"state": state, "state_at": store.now_iso()}
        if doing is not None:
            fields["doing"] = doing
        self.r.hset(store.k_member(self.room, self.agent), mapping=fields)
        store.touch_presence(self.r, self.room, self.agent)

    def wait(self):
        """Return (posts for this member, cursor to commit once handled).

        Unrelated posts advance the cursor immediately; posts for this member are committed only after the wake
        completes, so a worker killed mid-wake receives them again on respawn.
        """
        deadline = time.monotonic() + self.wait_secs
        while time.monotonic() < deadline:
            store.touch_presence(self.r, self.room, self.agent)
            delivered, last_seen = store.read_from_cursor(
                self.r, self.room, self.agent, block_ms=store.BLOCK_CHUNK_MS
            )
            delivered = [m for m in delivered if m["kind"] != ECHO or store.is_stop(m)]
            if delivered:
                return delivered, last_seen
            if last_seen:
                self.commit(last_seen)
        return [], None

    def commit(self, cursor):
        self.r.set(store.k_cursor(self.room, self.agent), cursor)

    def post(self, to, body, kind="msg"):
        self.r.xadd(store.k_stream(self.room), store.make_fields(self.agent, to, body, kind=kind))

    def record_usage(self, usage):
        key = store.k_member(self.room, self.agent)
        pipe = self.r.pipeline()
        pipe.hincrby(key, "tokens_in", usage["in"])
        pipe.hincrby(key, "tokens_cached", usage["cached"])
        pipe.hincrby(key, "tokens_out", usage["out"])
        pipe.hincrby(key, "turns", 1)
        if usage["cost"] is not None:
            pipe.hincrbyfloat(key, "cost_usd", usage["cost"])
        pipe.execute()

    # ---- one wake ------------------------------------------------------------

    def heartbeat(self, done):
        while not done.wait(self.heartbeat_secs):
            store.touch_presence(self.r, self.room, self.agent)

    def run_seat(self, prompt):
        sid = self.session()
        result = self.invoke(prompt, sid)
        if result.returncode and sid and resume_failed(result.stderr):
            self.drop_session()
            note = f"Your previous session could not be resumed. Run `swarm --room {self.room} history` to catch up.\n\n"
            result = self.invoke(note + prompt, "")
        return result

    def invoke(self, prompt, sid):
        argv, stdin, env = SEATS[self.seat["kind"]](self, prompt, sid)
        try:
            return subprocess.run(
                argv,
                input=stdin or "",
                capture_output=True,
                text=True,
                env=env,
                cwd=self.workspace,
                timeout=self.seat_timeout,
            )
        except subprocess.TimeoutExpired:
            return failed(argv, 124, f"seat timed out after {self.seat_timeout:.0f}s (SWARM_SEAT_TIMEOUT)")
        except OSError as exc:
            return failed(argv, 127, f"could not start the seat: {type(exc).__name__}: {exc}")

    def wake(self, msgs):
        self.set_state("working", doing="")
        head = self.stream_head()
        stop = threading.Event()
        threading.Thread(target=self.heartbeat, args=(stop,), daemon=True).start()
        try:
            result = self.run_seat(self.render(msgs))
        finally:
            stop.set()
        parsed = parse_output(result.stdout)
        self.save_session(parsed["session"] or self.session_after_first_wake())
        self.record_usage(parsed)
        if result.returncode:
            self.report_crash(result)
        elif self.replies_on_stdout() or not self.posted_since(head):
            body = (parsed["text"] or result.stdout or "").strip()
            if not is_silent_reply(body):
                self.post(senders(msgs), body or "(no output)", kind=auto_kind(msgs))
        self.set_state("waiting")

    def stream_head(self):
        last = self.r.xrevrange(store.k_stream(self.room), count=1)
        return last[0][0] if last else "0-0"

    def posted_since(self, head):
        entries = self.r.xrange(store.k_stream(self.room), f"({head}", "+")
        return any(fields.get("from") == self.agent for _, fields in entries)

    def session_after_first_wake(self):
        return self.session() or getattr(self, "_new_sid", "")

    def replies_on_stdout(self):
        return self.seat["kind"] == "agy" or os.environ.get("SWARM_SEAT_REPLY") == "stdout"

    def report_crash(self, result):
        tail = (result.stderr or result.stdout or "").strip()[-800:]
        self.post(
            self.coordinator,
            f"{self.agent}: seat `{self.harness}` exited {result.returncode}.\n{tail}",
            kind="system",
        )
        self.set_state("waiting", doing=f"crashed (exit {result.returncode})")

    def render(self, msgs):
        lines = [f"New posts for you on the swarm board (room {self.room}), oldest first:", ""]
        for m in msgs:
            topic = f" [{m['topic']}]" if m["topic"] else ""
            lines += [f"--- {m['id']} from {m['from']} to {m['to']}{topic}", m["body"], ""]
        lines.append(f"Reply on the board: swarm --room {self.room} send --from {self.agent} --to <who> -")
        return "\n".join(lines)

    def system_prompt(self):
        text = default_worker_prompt()
        if not text:
            return ""
        return (
            text.replace("{AGENT}", self.agent)
            .replace("{ROOM}", self.room)
            .replace("{COORDINATOR}", self.coordinator)
        )

    # ---- main loop -----------------------------------------------------------

    def run(self):
        self.lock()
        try:
            return self.loop()
        finally:
            self.unlock()

    def loop(self):
        self.set_state("waiting")
        while not self.should_stop():
            msgs, cursor = self.wait()
            finished = [m for m in msgs if self.ends_swarm(m)]
            if finished:
                self.commit(cursor)
                first_line = (finished[-1]["body"].strip().splitlines() or [""])[0][:200]
                self.set_state("retired", doing=first_line)
                return 0
            if msgs:
                self.wake_safely(msgs)
                self.commit(cursor)
        return 0

    def ends_swarm(self, msg):
        return store.ends_swarm(msg, self.r.hget(store.k_member(self.room, msg["from"]), "role"))

    def wake_safely(self, msgs):
        try:
            self.wake(msgs)
        except Exception as exc:
            self.report_crash(failed([], 1, f"swarm worker error: {type(exc).__name__}: {exc}"))


def auto_kind(msgs):
    """When a seat posts nothing itself, the worker posts its final text with kind 'auto'.
    If every message in the wake was already auto/echo, the reply is 'echo' so two non-posting seats stop."""
    return ECHO if all(m["kind"] in (AUTO, ECHO) for m in msgs) else AUTO


def is_silent_reply(text):
    """True when a seat explicitly opted out of replying on the board."""
    return (text or "").strip() == SILENT_REPLY


# ---- seats: (worker, prompt, session_id) -> (argv, stdin, env) ---------------

def base_env(w, sid):
    extra_dirs = [os.path.dirname(sys.executable)]
    if os.environ.get("SWARM_BIN"):
        extra_dirs.insert(0, os.path.dirname(os.path.abspath(os.environ["SWARM_BIN"])))
    raw_path = os.pathsep.join([*extra_dirs, os.environ.get("PATH", "")])
    # Strip terminal-multiplexer shims and env vars so headless seats don't inherit interactive hooks.
    path = os.pathsep.join(p for p in raw_path.split(os.pathsep) if p and "/cmux-cli-shims" not in p)
    env = {k: v for k, v in os.environ.items() if not k.startswith("CMUX_")}
    env.update(
        SWARM_AGENT=w.agent,
        SWARM_SESSION=sid,
        SWARM_ROOM=w.room,
        SWARM_REDIS_URL=getattr(w, "url", store.default_url()),
        PATH=path,
        CLAUDECODE="",
        CLAUDE_CODE_ENTRYPOINT="",
    )
    return env


def claude_seat(w, prompt, sid):
    flags = shlex.split(os.environ["SWARM_CLAUDE_FLAGS"]) if "SWARM_CLAUDE_FLAGS" in os.environ else w.claude_flags
    if sid:
        session = ["--resume", sid]
    else:
        w._new_sid = str(uuid.uuid4())
        session = ["--session-id", w._new_sid, "--append-system-prompt", w.system_prompt()]
    model = ["--model", w.model] if w.model else []
    argv = list(w.seat.get("launcher") or ["claude"]) + ["-p", "--output-format", "json", *session, *model, *flags]
    return argv, prompt, base_env(w, sid)


def codex_seat(w, prompt, sid):
    if sid:
        return ["codex", "exec", "resume", sid, "--json", "-"], prompt, base_env(w, sid)
    model = ["-m", w.model] if w.model else []
    effort = ["-c", f"model_reasoning_effort={w.effort}"] if w.effort else []
    argv = ["codex", "exec", "--json", "-s", "workspace-write", "--skip-git-repo-check", *model, *effort, "-"]
    return argv, w.system_prompt() + "\n\n" + prompt, base_env(w, sid)


def omp_seat(w, prompt, sid):
    session_dir = w.path("omp")
    if sid:
        return ["omp", "-p", "--mode", "json", "--session-dir", session_dir, "-c", prompt], None, base_env(w, sid)
    w._new_sid = session_dir
    model = ["--model", w.model] if w.model else []
    argv = ["omp", "-p", "--mode", "json", "--session-dir", session_dir, *model, w.system_prompt() + "\n\n" + prompt]
    return argv, None, base_env(w, sid)


def agy_seat(w, prompt, sid):
    cmd = list(w.seat.get("command") or ["agy"])
    model = ["-m", w.model] if w.model else []
    text = "\n\n".join([w.system_prompt(), AGY_NOTE, prompt, workspace_snapshot(w.workspace)])
    return cmd + model, text, base_env(w, sid)


def cmd_seat(w, prompt, sid):
    cmd = w.seat.get("command") or shlex.split(os.environ["SWARM_SEAT_CMD"])
    return list(cmd), prompt, base_env(w, sid)


SEATS = {
    "claude": claude_seat,
    "codex": codex_seat,
    "omp": omp_seat,
    "agy": agy_seat,
    "cmd": cmd_seat,
}


# ---- read-only workspace snapshot (for tool-less seats like agy) -------------

def git(workspace, *args):
    try:
        res = subprocess.run(["git", "-C", workspace, *args], capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return res.stdout if res.returncode == 0 else None


def snapshot_base(workspace):
    """Commit the member's work starts from: SWARM_BASE, else merge-base with the first ref that resolves."""
    head = (git(workspace, "rev-parse", "HEAD") or "").strip()
    for ref in filter(None, (os.environ.get("SWARM_BASE"),) + BASE_CANDIDATES):
        base = (git(workspace, "merge-base", "HEAD", ref) or "").strip()
        if base and base != head:
            return base
    return None


def committed_work(workspace):
    base = snapshot_base(workspace)
    if base:
        return f"----- commits since {base[:12]} -----\n" + (git(workspace, "log", "-p", "--reverse", f"{base}..HEAD") or "")
    shown = git(workspace, "show", "HEAD")
    return f"----- last commit -----\n{shown}" if shown else ""


def workspace_snapshot(workspace):
    """Committed work since base, uncommitted diff, and untracked files, capped at SNAPSHOT_LIMIT."""
    if not workspace or not os.path.isdir(workspace):
        return ""
    parts = [f"===== workspace {workspace} =====", committed_work(workspace)]
    diff = git(workspace, "diff", "HEAD")
    if diff:
        parts.append("----- uncommitted (git diff HEAD) -----\n" + diff)
    listed = git(workspace, "ls-files", "--others", "--exclude-standard")
    for name in (listed.split() if listed is not None else sorted(os.listdir(workspace))):
        parts.append(file_block(workspace, name))
    return truncate("\n".join(p for p in parts if p))


def truncate(text):
    if len(text) <= SNAPSHOT_LIMIT:
        return text
    cut = len(text) - SNAPSHOT_LIMIT
    return text[:SNAPSHOT_LIMIT] + f"\n===== snapshot truncated: {cut} more characters not shown =====\n"


def file_block(workspace, name):
    path = os.path.join(workspace, name)
    try:
        with open(path, errors="replace") as f:
            return f"----- {name} -----\n{f.read()}"
    except (IsADirectoryError, FileNotFoundError, PermissionError):
        return ""


# ---- JSON output & token usage parsing ---------------------------------------

def json_objects(text):
    for line in (text or "").splitlines():
        line = line.strip()
        if line.startswith("{"):
            try:
                yield json.loads(line)
            except ValueError:
                continue


def usage_tokens(usage):
    """(fresh in, cache reads, out). Claude separates cache reads; Codex counts them inside input."""
    codex_cached = usage.get("cached_input_tokens") or 0
    fresh = (usage.get("input_tokens") or 0) - codex_cached + (usage.get("cache_creation_input_tokens") or 0)
    cached = (usage.get("cache_read_input_tokens") or 0) + codex_cached
    return fresh, cached, usage.get("output_tokens") or 0


def omp_turn(obj):
    message = obj.get("message") or {}
    return message if obj.get("type") == "turn_end" and message.get("role") == "assistant" else None


def final_text(obj):
    if obj.get("type") == "result" and isinstance(obj.get("result"), str):
        return obj["result"]
    item = obj.get("item") or {}
    if obj.get("type") == "item.completed" and item.get("type") == "agent_message":
        return item.get("text") or ""
    turn = omp_turn(obj)
    if turn:
        return "".join(part.get("text", "") for part in turn.get("content") or [] if part.get("type") == "text")
    return ""


def usage_of(obj):
    if isinstance(obj.get("usage"), dict):
        cost = obj.get("total_cost_usd")
        return (*usage_tokens(obj["usage"]), None if cost is None else float(cost))
    usage = (omp_turn(obj) or {}).get("usage")
    if not isinstance(usage, dict):
        return None
    fresh = (usage.get("input") or 0) + (usage.get("cacheWrite") or 0)
    cost = (usage.get("cost") or {}).get("total")
    return fresh, usage.get("cacheRead") or 0, usage.get("output") or 0, None if cost is None else float(cost)


def parse_output(text):
    out = {"session": "", "in": 0, "cached": 0, "out": 0, "cost": None, "text": ""}
    for obj in json_objects(text):
        out["session"] = obj.get("session_id") or obj.get("thread_id") or out["session"]
        out["text"] = final_text(obj) or out["text"]
        usage = usage_of(obj)
        if usage:
            out["in"] += usage[0]
            out["cached"] += usage[1]
            out["out"] += usage[2]
            if usage[3] is not None:
                out["cost"] = (out["cost"] or 0) + usage[3]
    return out


def failed(argv, code, message):
    return subprocess.CompletedProcess(argv, code, stdout="", stderr=message)


def resume_failed(stderr):
    text = (stderr or "").lower()
    return any(s in text for s in RESUME_FAILED)


def senders(msgs):
    return ",".join(dict.fromkeys(m["from"] for m in msgs))


def run_worker(args):
    cfg = swarm_config.load()
    swarm_config.seat(cfg, args.harness)
    return Worker(args, cfg).run()
