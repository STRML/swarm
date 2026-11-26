"""Seat and model config for `swarm spawn` and `swarm worker`.

Reads $SWARM_CONFIG, else ~/.config/swarm/config.yaml (falling back to ~/.config/messagebus/swarm.yaml
if present). Without a file, built-in default seats apply.
"""
import os
import shlex

import yaml

DEFAULT_CLAUDE_FLAGS = (
    "--permission-mode acceptEdits --allowedTools Edit Write Read Glob Grep "
    "Bash(git:*) Bash(gh:*) Bash(swarm:*) Bash(python:*) Bash(pytest:*) Bash(ls:*) "
    "Bash(cat:*) Bash(mkdir:*)"
)
KINDS = ("claude", "codex", "omp", "agy", "cmd")
DEFAULT_SEATS = {
    "claude": {"kind": "claude", "launcher": ["claude"]},
    "codex": {"kind": "codex"},
    "omp": {"kind": "omp"},
    "agy": {"kind": "agy", "command": ["agy"]},
    "cmd": {"kind": "cmd"},
}


def config_path():
    if os.environ.get("SWARM_CONFIG"):
        return os.environ["SWARM_CONFIG"]
    primary = os.path.expanduser("~/.config/swarm/config.yaml")
    if os.path.exists(primary):
        return primary
    legacy = os.path.expanduser("~/.config/messagebus/swarm.yaml")
    return legacy if os.path.exists(legacy) else primary


def read_file(path):
    if not os.path.exists(path):
        return {}
    with open(path) as f:
        return yaml.safe_load(f) or {}


def load(path=None):
    raw = read_file(path or config_path())
    seats = dict(raw.get("seats") or DEFAULT_SEATS)
    seats.setdefault("cmd", DEFAULT_SEATS["cmd"])
    for name, entry in seats.items():
        if entry.get("kind") not in KINDS:
            raise SystemExit(f"swarm config: seat '{name}' has kind {entry.get('kind')!r}; use one of {KINDS}")
    return {
        "seats": seats,
        "picker": raw.get("picker"),
        "models": raw.get("models") or [],
        "roles": raw.get("roles") or {},
        "unknown_score": float(raw.get("unknown_score", 0)),
        "spread_penalty": float(raw.get("spread_penalty", 10)),
        "claude_flags": shlex.split(raw.get("claude_flags") or DEFAULT_CLAUDE_FLAGS),
    }


def writes(seat_cfg):
    return seat_cfg.get("writes", seat_cfg["kind"] != "agy")


def seat_score(cfg, scores, name):
    value = scores.get(name)
    if value == "throttled":
        return None
    return cfg["unknown_score"] if value is None else float(value)


def pool(cfg, name):
    """The quota pool a seat draws from. Seats sharing one subscription name the same pool."""
    return (cfg["seats"].get(name) or {}).get("pool", name)


def candidates(cfg, band, scores, load=None):
    """(headroom score, model index, seat name, model entry) for every seat serving a model in the band.

    `load` counts live members per pool; each one docks its pool by spread_penalty so parallel spawns spread
    across seats rather than piling onto whichever seat a cached quota reading favors.
    """
    load = load or {}
    out = []
    for model in cfg["models"]:
        if not band["min"] <= model["index"] <= band["max"]:
            continue
        for name in model["seats"]:
            seat_cfg = cfg["seats"].get(name)
            score = seat_score(cfg, scores, name) if seat_cfg else None
            if score is None or (band.get("writes", True) and not writes(seat_cfg)):
                continue
            score -= cfg.get("spread_penalty", 0) * load.get(pool(cfg, name), 0)
            out.append((score, model["index"], name, model))
    return out


def choose(cfg, role, scores, load=None):
    """Pick the seat with the most headroom among models in the role's intelligence band."""
    band = cfg["roles"].get(role)
    if not band:
        raise SystemExit(f"swarm config: no band for role '{role}'; add roles.{role}: {{min, max}}")
    if not all(isinstance(band.get(k), (int, float)) for k in ("min", "max")):
        raise SystemExit(f"swarm config: roles.{role} needs numeric min and max, got {band!r}")
    found = candidates(cfg, band, scores, load)
    if not found:
        raise SystemExit(f"swarm config: no unthrottled seat serves a model with index {band['min']}-{band['max']}")
    _, _, name, model = max(found, key=lambda c: (c[0], c[1]))
    return {"harness": name, "model": model["model"], "effort": model.get("effort")}


def seat(cfg, name):
    if name not in cfg["seats"]:
        raise SystemExit(
            f"swarm config: no seat '{name}' (have: {', '.join(sorted(cfg['seats']))}); edit {config_path()}"
        )
    return cfg["seats"][name]
