"""Spawn one swarm member: pick a seat, join the room, post its brief, launch a detached worker.

Exits 3 when that member's worker is already running; a member whose worker died is respawned with its
cursor kept. Exits 4 before joining anyone when run inside a macOS sandbox (children cannot drop Seatbelt).
"""
import ctypes
import json
import os
import subprocess
import sys

from swarm import config as swarm_config
from swarm import store

PICKER_TIMEOUT = 20  # safely under the 60s spawn lock timeout


def headroom(cfg):
    if not cfg["picker"]:
        return {}
    try:
        out = subprocess.run(list(cfg["picker"]), capture_output=True, text=True, check=True, timeout=PICKER_TIMEOUT)
    except subprocess.TimeoutExpired:
        raise SystemExit(f"swarm spawn: picker {cfg['picker']} took over {PICKER_TIMEOUT}s; pass --harness or fix it")
    return json.loads(out.stdout.strip().splitlines()[-1])


def room_load(r, cfg, room, agent):
    """Live members per quota pool in this room (excluding the member being respawned)."""
    load = {}
    for key in r.scan_iter(match=store.k_member(room, "*")):
        name = key.rsplit(":", 1)[-1]
        member = r.hgetall(key)
        if name == agent or member.get("state") == "retired" or not member.get("harness"):
            continue
        if not r.exists(store.k_presence(room, name)):
            continue
        seat_pool = swarm_config.pool(cfg, member["harness"])
        load[seat_pool] = load.get(seat_pool, 0) + 1
    return load


def pick_seat(cfg, args, r):
    if args.harness:
        return {"harness": args.harness, "model": args.model, "effort": args.effort}
    chosen = swarm_config.choose(cfg, args.role, headroom(cfg), room_load(r, cfg, args.room, args.agent))
    return {
        "harness": chosen["harness"],
        "model": args.model or chosen["model"],
        "effort": args.effort or chosen["effort"],
    }


def state_dir(room):
    path = os.path.join(store.state_root(), room)
    os.makedirs(path, exist_ok=True)
    return path


def sandboxed():
    """True inside a macOS Seatbelt sandbox. Children inherit it and cannot drop it."""
    if sys.platform != "darwin":
        return False
    return ctypes.CDLL("/usr/lib/libSystem.B.dylib").sandbox_check(os.getpid(), None, 0) != 0


def refuse_if_sandboxed():
    if not sandboxed():
        return
    sys.stderr.write(
        "swarm spawn: running inside a sandbox, which every member would inherit; nothing was spawned. "
        "Re-run swarm spawn with the sandbox disabled.\n"
    )
    sys.exit(4)


def refuse_if_running(room, agent):
    try:
        pid = int(open(os.path.join(state_dir(room), f"{agent}.pid")).read().strip())
        os.kill(pid, 0)
    except (FileNotFoundError, ValueError, ProcessLookupError, PermissionError):
        return
    sys.stderr.write(f"swarm spawn: {agent} is already running in room={room} (pid {pid})\n")
    sys.exit(3)


def join_or_rejoin(r, args, seat):
    """First spawn joins with --replay. A respawn keeps the existing cursor so work resumes where it stopped."""
    if r.exists(store.k_member(args.room, args.agent)):
        fields = {
            "role": args.role,
            "harness": seat["harness"],
            "model": seat["model"],
            "workspace": args.workspace,
        }
        r.hset(store.k_member(args.room, args.agent), mapping={k: v for k, v in fields.items() if v})
        if args.group:
            r.sadd(store.k_groups(args.room, args.agent), *args.group)
        store.touch_presence(r, args.room, args.agent)
        return
    store.join_member(
        r,
        args.room,
        args.agent,
        replay=True,
        groups=args.group,
        role=args.role,
        harness=seat["harness"],
        model=seat["model"],
        workspace=args.workspace,
    )


def post_brief(r, args):
    if not args.brief:
        return
    text = sys.stdin.read() if args.brief == "-" else open(args.brief).read()
    r.xadd(store.k_stream(args.room), store.make_fields(args.frm, "@" + args.agent, text.strip(), topic="brief"))


def start_worker(args, seat):
    log = os.path.join(state_dir(args.room), f"{args.agent}.log")
    argv = [
        sys.executable, "-m", "swarm",
        "--url", args.url,
        "--room", args.room,
        "worker", args.agent, seat["harness"],
    ]
    for flag, value in (("--model", seat["model"]), ("--effort", seat["effort"]), ("--workspace", args.workspace)):
        if value:
            argv += [flag, value]
    env = dict(os.environ, SWARM_REDIS_URL=args.url, SWARM_ROOM=args.room, SWARM_COORDINATOR=args.frm)
    if args.base:
        env["SWARM_BASE"] = args.base
    with open(log, "a") as out:
        proc = subprocess.Popen(
            argv,
            stdout=out,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            env=env,
            start_new_session=True,
        )
    return proc.pid, log


def run_spawn(args):
    if not getattr(args, "room_given", False):
        sys.stderr.write("swarm spawn: --room is required (joining replays the room's broadcasts)\n")
        return 2
    refuse_if_sandboxed()
    refuse_if_running(args.room, args.agent)
    cfg = swarm_config.load()
    r = store.connect(args.url)
    # Pick and join under one per-room lock so parallel spawns see each other and spread across seats.
    with r.lock(store.k_spawn_lock(args.room), timeout=60, blocking_timeout=60):
        seat = pick_seat(cfg, args, r)
        swarm_config.seat(cfg, seat["harness"])
        join_or_rejoin(r, args, seat)
    post_brief(r, args)
    pid, log = start_worker(args, seat)
    print(json.dumps({
        "agent": args.agent,
        "harness": seat["harness"],
        "model": seat["model"],
        "pid": pid,
        "log": log,
    }))
    return 0
