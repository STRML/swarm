"""`swarm init`: write a starter config for the harnesses on PATH and optionally link the bundled skills."""
import json
import os
import shutil
import sys
from pathlib import Path

import yaml

from swarm import config as swarm_config
from swarm import store

HARNESSES = ("claude", "codex", "omp", "agy")
# Starter models, as in examples/config.yaml. A harness without an entry is used through `spawn --harness`.
STARTER_MODELS = {
    "claude": {"model": "claude-sonnet-5-5", "index": 55},
    "codex": {"model": "gpt-6.2-terra", "index": 57, "effort": "high"},
    "agy": {"model": "gemini-4-neon-xhigh", "index": 61},
}
# The verifier band starts low enough that any single detected harness can fill it.
STARTER_ROLES = {"worker": {"min": 54, "max": 58}, "verifier": {"min": 55, "max": 62, "writes": False}}
SKILL_LINKS = {"swarm": "swarm", "rooms": "swarm-rooms"}
SKILL_HOMES = {".claude": "claude", ".gemini": "agy"}


def detected_harnesses():
    return [name for name in HARNESSES if shutil.which(name)]


def starter_seats(detected):
    names = [*detected, "cmd"] if detected else list(swarm_config.DEFAULT_SEATS)
    return {name: swarm_config.DEFAULT_SEATS[name] for name in names}


def _section(comment, key, value):
    # None puts leaf collections inline; a scalar section needs block style or it renders as `{key: value}`.
    flow = None if isinstance(value, (dict, list)) else False
    body = yaml.safe_dump({key: value}, sort_keys=False, default_flow_style=flow, width=100)
    return f"# {comment}\n{body}"


def render_config(seats):
    models = [{**model, "seats": [name]} for name, model in STARTER_MODELS.items() if name in seats]
    return "\n".join([
        "# Seat, model and role config for `swarm spawn` and `swarm worker`. Written by `swarm init`.\n"
        "# docs/seats.md describes every option, including quota pickers and shared pools.\n",
        _section("How to launch each harness. `cmd` runs $SWARM_SEAT_CMD.", "seats", seats),
        _section("Models `swarm spawn` may pick: a capability score and the seats that run each.", "models", models),
        _section("Score band per role. `writes: false` also allows read-only seats.", "roles", STARTER_ROLES),
        _section(
            "Extra flags for every claude seat (headless runs cannot answer prompts).",
            "claude_flags", swarm_config.DEFAULT_CLAUDE_FLAGS,
        ),
    ])


def target_path():
    """$SWARM_CONFIG, else the primary path. The legacy messagebus file belongs to another tool."""
    return os.environ.get("SWARM_CONFIG") or os.path.expanduser("~/.config/swarm/config.yaml")


def write_config(path, text, force):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w" if force else "x", encoding="utf-8") as f:
        f.write(text)


def redis_status(url):
    try:
        store.connect(url)
    except Exception as exc:  # noqa: BLE001
        return {"url": url, "ok": False, "error": str(exc)}
    return {"url": url, "ok": True}


# ---- skills ----------------------------------------------------------------

def bundled_skills():
    """The checkout's skills directory, or None for an install that ships without it."""
    root = Path(__file__).resolve().parents[2] / "skills"
    return root if all((root / name / "SKILL.md").is_file() for name in SKILL_LINKS) else None


def skill_directories(detected):
    home = Path(os.environ.get("SWARM_HOME") or os.path.expanduser("~"))
    wanted = [
        home / folder / "skills"
        for folder, harness in SKILL_HOMES.items()
        if harness in detected or (home / folder).is_dir()
    ]
    return wanted or [home / ".claude" / "skills"]


def link_skill(source, link, force):
    """Point `link` at `source` and say what happened. Real files and directories are never removed."""
    existed = link.is_symlink()
    if existed and link.resolve() == source.resolve():
        return "already linked"
    if existed and not force:
        return "skipped: links elsewhere (--force replaces it)"
    if not existed and link.exists():
        return "skipped: not a symlink"
    link.parent.mkdir(parents=True, exist_ok=True)
    if existed:
        link.unlink()
    link.symlink_to(source, target_is_directory=True)
    return "replaced" if existed else "linked"


def link_skills(root, detected, force):
    results = []
    for directory in skill_directories(detected):
        for source_name, link_name in SKILL_LINKS.items():
            source, link = root / source_name, directory / link_name
            try:
                status = link_skill(source, link, force)
            except OSError as exc:
                status = f"failed: {exc.strerror}"
            results.append({"link": str(link), "source": str(source), "status": status})
    return results


# ---- command ---------------------------------------------------------------

def print_summary(report):
    redis_info = report["redis"]
    redis_line = "ok" if redis_info["ok"] else f"unreachable ({redis_info['error']})"
    print(f"config:  wrote {report['config']}")
    print(f"seats:   {', '.join(report['seats'])}")
    if not report["detected"]:
        print("         (no harness found on PATH, so every default seat was written)")
    print(f"redis:   {redis_line}  [{redis_info['url']}]")
    for skill in report["skills"]:
        print(f"skill:   {skill['link']}  {skill['status']}")
    for note in report["notes"]:
        print(f"note:    {note}")


def refuse(message):
    sys.stderr.write(f"swarm init: {message}\n")
    return 1


def run_init(args):
    existing = swarm_config.config_path()
    if os.path.exists(existing) and not args.force:
        return refuse(f"{existing} already exists; pass --force to write a new config")
    path = target_path()
    detected = detected_harnesses()
    seats = starter_seats(detected)
    try:
        write_config(path, render_config(seats), args.force)
    except OSError as exc:
        return refuse(f"cannot write {path}: {exc.strerror}")

    notes = [f"{existing} is left untouched; {path} now takes precedence"] if existing != path else []
    skills = []
    if args.link_skills:
        root = bundled_skills()
        if root:
            skills = link_skills(root, detected, args.force)
        else:
            notes.append("this install has no bundled skills; clone the repo and re-run from the checkout")
    report = {
        "config": path,
        "detected": detected,
        "seats": list(seats),
        "redis": redis_status(args.url),
        "skills": skills,
        "notes": notes,
    }
    if args.json:
        print(json.dumps(report, indent=2))
    else:
        print_summary(report)
    return 1 if any(s["status"].startswith("failed") for s in skills) else 0
