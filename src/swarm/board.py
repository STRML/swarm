"""Terminal board and room picker for swarm rooms.

Run `swarm board` (no --room) for the live room picker, or `swarm board --room R` for one room's live board.
In the picker, `a` archives, `x` clears (after `y`), and idle rooms are swept on open. A board writes only on
request: `m` posts a message as `operator`, `d` (after `y`) ends the swarm. `l` peeks at a worker log, and `b`
returns to the picker.
"""
import argparse
import contextlib
import io
import os
import re
import shlex
import subprocess
import sys
import textwrap
import time
import zlib
from dataclasses import dataclass, field
from datetime import datetime

import redis
from rich.console import Console
from rich.style import Style
from rich.text import Text
from textual import events
from textual.app import App
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.css.query import NoMatches
from textual.widgets import DataTable, Input, Rule, Static

from swarm import store

PALETTE = ["#ff7b72", "#7ee787", "#79c0ff", "#d2a8ff", "#56d4dd", "#ff9bce", "#f0883e", "#c9a27e", "#d8dee9"]
GLYPHS = "◆▲■✦⬢◗"
GREEN, DIM, BLUE, GOLD = "#3fb950", "#8b949e", "#79c0ff", "#f2cc60"
SELECTED_BG = "on #1f334d"
UNFOCUSED_BG = "on #11161d"
CARD_COLUMNS = 3
CARD_LINES = 4
AVATAR_WIDTH = 5
AVATAR_MIN_COLW = 24
ANIM_INTERVAL = 0.35
DOING_STYLE = "italic #8b949e"

# 4x5 single-width Unicode avatars per model/harness family:
#   gemini/agy -> little bee, claude -> Clawd mascot, codex/gpt -> rotating hex-knot,
#   deepseek -> spouting whale, omp/glm/kimi -> pulsing orbital node, generic -> diamond.
# Each vendor has a palette of distinct tints so parallel workers of the same vendor look distinct,
# brisk working frames, and calm half-speed sleeping frames (`z`/`Z`).
AVATARS = {
    "gemini": {
        "colors": ("#f2cc60", "#ffd866", "#f0b429", "#ffe082"),
        "accent": "#79c0ff",
        "working": (
            ("╲ ✧ ╱", "╭█═█╮", "╰█═█╯", " · · "),
            ("  ✦  ", "═█═█═", "╰█═█╯", " · · "),
            ("  ·  ", "╭█═█╮", "╱█═█╲", " · · "),
            ("  ✧  ", "═█═█═", "╰█═█╯", " · · "),
        ),
        "waiting": (
            ("   z ", "╭█═█╮", "╰█═█╯", "     "),
            ("  zZ ", "╭█═█╮", "╰█═█╯", "     "),
            ("   Z ", "╭█═█╮", "╰█═█╯", "     "),
            ("   · ", "╭█═█╮", "╰█═█╯", "     "),
        ),
        "dim":     ("     ", "╭█═█╮", "╰█═█╯", "     "),
    },
    "claude": {
        "colors": ("#d97757", "#e8916f", "#cc6b49", "#f0a07c"),
        "accent": "#f2cc60",
        "working": (
            ("  ✦  ", "▐▛▀▜▌", "▐███▌", " ▘ ▝ "),
            ("▐▛▀▜▌", "▐███▌", " ▘ ▝ ", "  ·  "),
            ("  ✧  ", "▐▛─▜▌", "▐███▌", " ▘ ▝ "),
            ("  ·  ", "▐▛▀▜▌", "▐███▌", "▘   ▝"),
        ),
        "waiting": (
            ("   z ", "▐▛─▜▌", "▐███▌", " ▘ ▝ "),
            ("  zZ ", "▐▛─▜▌", "▐███▌", " ▘ ▝ "),
            ("   Z ", "▐▛─▜▌", "▐███▌", " ▘ ▝ "),
            ("   · ", "▐▛─▜▌", "▐███▌", " ▘ ▝ "),
        ),
        "dim":     ("     ", "▐▛─▜▌", "▐███▌", " ▘ ▝ "),
    },
    "codex": {
        "colors": ("#10a37f", "#34d399", "#2dd4bf", "#4ade80"),
        "accent": "#7ee787",
        "working": (
            (" ╭─╮ ", "╱ ◎ ╲", "╲ ◉ ╱", " ╰─╯ "),
            (" ╭◈╮ ", "│╲ ╱│", "│╱ ╲│", " ╰◈╯ "),
            (" ╭─╮ ", "╱ ◉ ╲", "╲ ◎ ╱", " ╰─╯ "),
            (" ╭·╮ ", "│╱ ╲│", "│╲ ╱│", " ╰·╯ "),
        ),
        "waiting": (
            ("   z ", " ╭─╮ ", " │─│ ", " ╰─╯ "),
            ("  zZ ", " ╭─╮ ", " │─│ ", " ╰─╯ "),
            ("   Z ", " ╭─╮ ", " │─│ ", " ╰─╯ "),
            ("   · ", " ╭─╮ ", " │─│ ", " ╰─╯ "),
        ),
        "dim":     (" ╭─╮ ", "│ · │", "│   │", " ╰─╯ "),
    },
    "deepseek": {
        "colors": ("#58a6ff", "#4d6bfe", "#38bdf8", "#60a5fa"),
        "accent": "#56d4dd",
        "working": (
            (" ╲│╱ ", "╭───╮", "█ ◉ █", "╰═══╯"),
            ("· · ·", "╭───╮", "█ ◉ █", "╰═══╯"),
            ("  │  ", "╭───╮", "█ ◉ █", "╰═~═╯"),
            (" ·│· ", "╭───╮", "█ ◉ █", "╰~═~╯"),
        ),
        "waiting": (
            ("   z ", "╭───╮", "█ ─ █", "╰═══╯"),
            ("  zZ ", "╭───╮", "█ ─ █", "╰═══╯"),
            ("   Z ", "╭───╮", "█ ─ █", "╰═══╯"),
            ("   · ", "╭───╮", "█ ─ █", "╰═══╯"),
        ),
        "dim":     ("     ", "╭───╮", "█ ─ █", "╰═══╯"),
    },
    "omp": {
        "colors": ("#d2a8ff", "#c084fc", "#a78bfa", "#e879f9"),
        "accent": "#56d4dd",
        "working": (
            ("  ·  ", "╭─◈─╮", "╰─○─╯", "  ·  "),
            ("  ✦  ", "╭─○─╮", "╰─◈─╯", "  ✧  "),
            (" · · ", "╭─◉─╮", "╰─◉─╯", " · · "),
            ("  ✧  ", "╭─◈─╮", "╰─○─╯", "  ✦  "),
        ),
        "waiting": (
            ("   z ", "╭─○─╮", "╰─○─╯", "     "),
            ("  zZ ", "╭─○─╮", "╰─○─╯", "     "),
            ("   Z ", "╭─○─╮", "╰─○─╯", "     "),
            ("   · ", "╭─○─╮", "╰─○─╯", "     "),
        ),
        "dim":     ("     ", "╭─·─╮", "╰─·─╯", "     "),
    },
    "generic": {
        "colors": (),
        "accent": None,
        "working": (
            ("  ✦  ", " ╱◇╲ ", " ╲◇╱ ", "  ·  "),
            ("  ·  ", " ╱◈╲ ", " ╲◈╱ ", "  ✧  "),
            ("  ✧  ", " ╱◆╲ ", " ╲◆╱ ", "  ·  "),
            ("  ·  ", " ╱◈╲ ", " ╲◈╱ ", "  ✦  "),
        ),
        "waiting": (
            ("   z ", " ╱◇╲ ", " ╲◇╱ ", "     "),
            ("  zZ ", " ╱◇╲ ", " ╲◇╱ ", "     "),
            ("   Z ", " ╱◇╲ ", " ╲◇╱ ", "     "),
            ("   · ", " ╱◇╲ ", " ╲◇╱ ", "     "),
        ),
        "dim":     ("     ", " ╱·╲ ", " ╲·╱ ", "     "),
    },
}

SENDER_CELL_WIDTH = 28
MIN_RECIPIENT_WIDTH = 4
ROSTER_ROWS = 2
EXPANDED_CAP = 20
PAGE_FALLBACK = 10
HINTS = (
    "←→↑↓ move · tab pane · m msg · l log · d done · ⏎ hide · space expand · "
    "p filter · o open · f follow · z zoom · b rooms"
)
OPERATOR = "operator"
COMPOSE_HINT = "message · tab: recipient · @name or /to a,b · enter sends · esc cancels"
NOTICE_SECONDS = 5
ERROR_STYLE = "bold #ff7b72"
LOG_TAIL_LINES = 200
LOG_TAIL_BYTES = 128 * 1024
WHEEL_STEP = 3
BLOCKED_WHILE_COMPOSING = frozenset({
    "compose", "expand", "filter", "finish", "leave", "log", "move", "move_horiz", "open_workspace", "page",
    "picker", "toggle_follow", "toggle_message", "zoom",
})
PICKER_HINTS = "enter open board · a archive · x clear · r refresh · q quit"
PICKER = "picker"
KIND_TAGS = {"done": ("✓ done", f"bold {GREEN}"), "system": ("⚙", DIM), "question": ("?", "bold #e3b341")}
PANES = ("roster", "posts", "message")
CONSOLE = Console(file=io.StringIO(), width=200)
TOKEN = re.compile(
    r"`([^`\n]+)`"
    r"|\*\*([^*\n]+)\*\*"
    r"|(?<![\w.])(@[\w.\-]*\w)"
    r"|((?:~|\.{0,2})/[\w.\-+/]+|\b[\w.\-]+(?:/[\w.\-]+)+)"
)
BULLET = re.compile(r"^(\s*(?:[-*+•]|\d+[.)])\s+)(.*)$")
HEADING = re.compile(r"^#{1,6}\s+")


def vendor_of(info):
    """Identify the avatar family from a member's model (preferred) or harness."""
    model = (info.get("model") or "").lower()
    harness = (info.get("harness") or "").lower()
    for text in (model, harness):
        if not text:
            continue
        if any(k in text for k in ("deepseek", "ds4", "studio-local", "studio/")):
            return "deepseek"
        if any(k in text for k in ("gemini", "agy", "antigravity", "hydrogen", "neon", "carbon", "argon")):
            return "gemini"
        if any(k in text for k in ("claude", "haiku", "sonnet", "opus", "fable", "anthropic")):
            return "claude"
        if any(k in text for k in ("codex", "gpt", "openai", "luna", "terra", "sol", "astra")):
            return "codex"
        if any(k in text for k in ("omp", "glm", "kimi", "neuralwatt")):
            return "omp"
    return "generic"


def avatar_color(state, name, spec):
    colors = spec["colors"]
    if not colors:
        return state.color(name)
    idx = state.colors.get(name)
    slot = idx if idx is not None else member_hash(name)
    return colors[slot % len(colors)]


def avatar_lines(state, name, frame=0):
    """Return 4 Rich Text lines (each 5 cells wide) for a member's animated avatar."""
    info = state.info(name)
    spec = AVATARS[vendor_of(info)]
    status = state.status(name)
    body_style = avatar_color(state, name, spec)
    accent_style = spec["accent"] or body_style
    phase = member_hash(name) % 4
    if status == "working":
        rows = spec["working"][(frame + phase) % len(spec["working"])]
        return [
            Text(rows[0], style=accent_style),
            Text(rows[1], style=Style(color=body_style, bold=True)),
            Text(rows[2], style=body_style),
            Text(rows[3], style=accent_style),
        ]
    if status == "waiting":
        rows = spec["waiting"][((frame // 2) + phase) % len(spec["waiting"])]
        return [
            Text(rows[0], style=DIM),
            Text(rows[1], style=body_style),
            Text(rows[2], style=body_style),
            Text(rows[3], style=DIM),
        ]
    return [Text(row, style=DIM) for row in spec["dim"]]


def clean(value):
    return store.terminal_text(value).replace("\t", "    ")


def member_hash(name):
    return zlib.crc32(name.encode("utf-8", "replace"))


def short_count(value):
    if value < 1000:
        return str(int(value))
    for size, unit in ((1_000_000, "M"), (1000, "k")):
        if value >= size:
            scaled = value / size
            return f"{scaled:.0f}{unit}" if scaled >= 100 else f"{scaled:.1f}".removesuffix(".0") + unit


def usage_text(tokens, cost, cached=0):
    parts = []
    if tokens:
        parts.append(f"{short_count(tokens)} tok")
    if cached:
        parts.append(f"+{short_count(cached)} cached")
    if cost:
        parts.append(f"${cost:.2f}")
    return " · ".join(parts)


def to_number(value, kind=int):
    try:
        return kind(float(value))
    except (TypeError, ValueError, OverflowError):
        return kind(0)


def ellipsize(value, width):
    if width <= 0:
        return ""
    return value if len(value) <= width else value[: max(width - 1, 0)] + "…"


def fit(text, width):
    text.truncate(width, overflow="ellipsis", pad=True)
    return text


def hhmmss(ms):
    return time.strftime("%H:%M:%S", time.localtime(ms / 1000))


def plural(count, word):
    return f"{count} {word}" if count == 1 else f"{count} {word}s"


def compact_age(secs):
    secs = max(int(secs), 0)
    for size, unit in ((86400, "d"), (3600, "h"), (60, "m")):
        if secs >= size:
            return f"{secs // size}{unit}"
    return f"{secs}s"


def fmt_when(ms, now):
    then = time.localtime(ms / 1000)
    if time.strftime("%Y%m%d", then) == time.strftime("%Y%m%d", time.localtime(now)):
        return time.strftime("%H:%M", then)
    return time.strftime("%b %d %H:%M", then)


def kind_tag(kind):
    tag = KIND_TAGS.get(kind)
    return Text(tag[0], style=tag[1]) if tag else None


def ago(ms, now):
    return compact_age(now - ms / 1000) + " ago"


def since(iso, now):
    try:
        then = datetime.fromisoformat(iso).timestamp()
    except (TypeError, ValueError):
        return ""
    return compact_age(now - then)


def keep_visible(top, first, last, height):
    if first < top:
        return first
    if last >= top + height:
        return min(first, last - height + 1)
    return top


def stream_key(msg_id):
    ms, _, seq = msg_id.partition("-")
    return (int(ms) if ms.isdigit() else -1, int(seq) if seq.isdigit() else 0)


def clamp(value, low, high):
    return max(low, min(value, high))


@dataclass
class Post:
    seq: int
    id: str
    ms: int
    sender: str
    to: str
    body: str
    kind: str = "msg"


@dataclass
class Snapshot:
    title: str = ""
    members: dict = field(default_factory=dict)
    entries: list = field(default_factory=list)
    reset: bool = False


def fetch_title(r, room):
    return r.hget(store.k_meta(room), "title") or ""


def fetch_member_names(r, room):
    names = set()
    for pattern in (store.k_member(room, "*"), store.k_presence(room, "*")):
        names |= {key.rsplit(":", 1)[-1] for key in r.scan_iter(match=pattern)}
    return sorted(names)


def fetch_members(r, room):
    names = fetch_member_names(r, room)
    pipe = r.pipeline()
    for name in names:
        pipe.hgetall(store.k_member(room, name))
        pipe.smembers(store.k_groups(room, name))
        pipe.exists(store.k_presence(room, name))
    results = pipe.execute()
    members = {}
    for i, name in enumerate(names):
        info, groups, present = results[3 * i:3 * i + 3]
        info.pop("replay_floor", None)
        members[name] = {**info, "groups": sorted(groups), "present": bool(present)}
    return members


def fetch_entries(r, room, last_id):
    entries = []
    while True:
        batch = r.xrange(store.k_stream(room), min=last_id, max="+", count=500)
        batch = [item for item in batch if item[0] != last_id]
        if not batch:
            return entries
        entries.extend(batch)
        last_id = batch[-1][0]


def fetch(r, room, last_id, loaded):
    reset = r.xlen(store.k_stream(room)) < loaded
    if reset:
        last_id = "-"
    entries = fetch_entries(r, room, last_id)
    return Snapshot(fetch_title(r, room), fetch_members(r, room), entries, reset)


class BoardState:
    def __init__(self, room):
        self.room = room
        self.title = ""
        self.members = {}
        self.posts = []
        self.last_id = "-"
        self.follow = True
        self.filter = None
        self.sel = 0
        self.member = None
        self.expanded = set()
        self.focus = "posts"
        self.show_message = True
        self.zoom = False
        self.post_top = 0
        self.roster_top = 0
        self.msg_top = 0
        self.msg_seq = None
        self.error = ""
        self.colors = {}
        self.post_rows = None
        self.line_seqs = []
        self.frame = 0
        self.compose_default = None
        self.compose_to = "all"
        self.pending_finish = False
        self.notice = None
        self.notice_until = 0.0
        self.show_log = False
        self.log_return = "posts"
        self.log_name = None
        self.log_lines = []
        self.log_width = 0
        self.log_top = None
        self.log_max = 0

    def ingest(self, snap):
        if snap.reset:
            self.posts, self.last_id = [], "-"
            self.expanded.clear()
        for msg_id, fields in self.unseen(snap.entries):
            self.posts.append(self.make_post(len(self.posts) + 1, msg_id, fields))
            self.last_id = msg_id
        self.title = clean(snap.title)
        self.members = {clean(name): self.clean_info(info) for name, info in snap.members.items()}
        self.error = ""
        self.assign_colors()
        self.settle_selection()

    def unseen(self, entries):
        floor = stream_key(self.last_id)
        return [(i, f) for i, f in entries if stream_key(i) > floor]

    @staticmethod
    def make_post(seq, msg_id, fields):
        msg = store.fields_to_msg(msg_id, fields)
        return Post(
            seq,
            msg_id,
            int(msg_id.split("-")[0]),
            clean(msg["from"]),
            clean(msg["to"]),
            clean(msg["body"]),
            clean(msg["kind"]),
        )

    @staticmethod
    def clean_info(info):
        cleaned = {key: clean(value) if isinstance(value, str) else value for key, value in info.items()}
        cleaned["groups"] = [clean(g) for g in info.get("groups", [])]
        return cleaned

    def assign_colors(self):
        for name in self.roster():
            if name not in self.colors and not self.is_coordinator(name):
                self.colors[name] = len(self.colors)

    def is_coordinator(self, name):
        return self.info(name).get("role") == "coordinator"

    def color(self, name):
        if self.is_coordinator(name):
            return GOLD
        index = self.colors.get(name)
        if index is None or index >= len(PALETTE):
            return PALETTE[member_hash(name) % len(PALETTE)]
        return PALETTE[index]

    def glyph(self, name):
        index = self.colors.get(name)
        if index is None:
            return GLYPHS[(member_hash(name) >> 8) % len(GLYPHS)]
        return GLYPHS[index % len(GLYPHS)]

    def knows(self, name):
        return name in self.roster()

    def member_usage(self, name):
        info = self.info(name)
        tokens = to_number(info.get("tokens_in")) + to_number(info.get("tokens_out"))
        return tokens, to_number(info.get("cost_usd"), float), to_number(info.get("tokens_cached"))

    def total_usage(self):
        usages = [self.member_usage(n) for n in self.roster()]
        return tuple(sum(u[i] for u in usages) for i in range(3))

    def doing(self, name):
        doing = self.info(name).get("doing", "").strip()
        if doing:
            return doing
        post = self.last_post_of(name)
        return post.body.strip().split("\n", 1)[0] if post else ""

    def settle_selection(self):
        visible = self.visible()
        if self.follow and visible:
            self.sel = len(visible) - 1
        self.sel = clamp(self.sel, 0, max(len(visible) - 1, 0))

    def visible(self):
        if self.filter is None:
            return self.posts
        return [p for p in self.posts if p.sender == self.filter]

    def selected_post(self):
        visible = self.visible()
        return visible[self.sel] if visible else None

    def roster(self):
        names = set(self.members) | {p.sender for p in self.posts}
        return sorted(names, key=lambda n: (self.info(n).get("role") != "coordinator", n))

    def info(self, name):
        return self.members.get(name, {})

    def retired(self):
        return [n for n in self.roster() if self.info(n).get("state") == "retired"]

    def active(self):
        return [n for n in self.roster() if self.info(n).get("state") != "retired"]

    def waiting_count(self):
        return sum(1 for n in self.active() if not self.is_working(n))

    def common_model(self):
        models = {self.info(n).get("model") or "" for n in self.active()}
        return models.pop() if len(models) == 1 else ""

    def finished(self):
        if not self.posts:
            return False
        last = self.posts[-1]
        to_everyone = "all" in last.to.split(",")
        return to_everyone and store.ends_swarm({"body": last.body, "kind": last.kind}, self.info(last.sender).get("role"))

    def status(self, name):
        info = self.info(name)
        if info.get("state") == "retired":
            return "retired"
        if info.get("state") != "working":
            return "waiting"
        return "working" if info.get("present", True) else "gone"

    def is_working(self, name):
        return self.status(name) == "working"

    def member_name(self):
        if self.member in self.roster():
            return self.member
        names = self.active() or self.roster()
        return names[0] if names else None

    def last_post_of(self, name):
        return next((p for p in reversed(self.posts) if p.sender == name), None)

    def move_member_grid(self, dx, dy):
        """Move selection in the CARD_COLUMNS roster grid."""
        names = self.active()
        if not names:
            return
        current = self.member_name()
        index = names.index(current) if current in names else 0
        if dx:
            target = clamp(index + dx, 0, len(names) - 1)
        elif dy:
            if len(names) <= CARD_COLUMNS:
                target = clamp(index + dy, 0, len(names) - 1)
            else:
                row, col = divmod(index, CARD_COLUMNS)
                max_row = (len(names) - 1) // CARD_COLUMNS
                new_row = clamp(row + dy, 0, max_row)
                target = min(new_row * CARD_COLUMNS + col, len(names) - 1)
        else:
            target = index
        self.member = names[target]

    def move_post(self, delta):
        self.select_post(self.sel + delta)

    def select_post(self, index):
        visible = self.visible()
        if not visible:
            return
        self.sel = clamp(index, 0, len(visible) - 1)
        self.follow = self.follow and self.sel == len(visible) - 1
        self.member = visible[self.sel].sender

    @property
    def composing(self):
        return self.compose_default is not None

    def default_recipient(self):
        post = self.selected_post()
        name = self.member_name() if self.focus == "roster" else post and post.sender
        try:
            return store.recipients(f"@{name}") if name else "all"
        except argparse.ArgumentTypeError:
            return "all"

    def open_compose(self):
        self.compose_default = self.compose_to = self.default_recipient()

    def close_compose(self):
        self.compose_default = None

    def toggle_recipient(self):
        self.compose_to = "all" if self.compose_to == self.compose_default else self.compose_default

    def coordinator_name(self):
        return next(filter(self.is_coordinator, self.roster()), "coordinator")

    def toggle_log(self):
        if self.show_log:
            self.close_log()
        elif self.member_name() is not None:
            self.show_log, self.log_name = True, None
            self.log_return, self.focus = self.focus, "message"

    def close_log(self):
        self.show_log = False
        if self.zoom or self.focus != "message":
            return
        self.focus = self.log_return
        if self.focus == "message" and not self.show_message:
            self.focus = "posts"

    def scroll_message(self, delta):
        if not self.show_log:
            self.msg_top = max(self.msg_top + delta, 0)
            return
        top = self.log_max if self.log_top is None else self.log_top
        top = max(top + delta, 0)
        self.log_top = None if top >= self.log_max else top

    def toggle_follow(self):
        self.follow = not self.follow
        self.settle_selection()

    def cycle_filter(self):
        order = [None, *self.roster()]
        index = order.index(self.filter) if self.filter in order else 0
        self.set_filter(order[(index + 1) % len(order)])

    def set_filter(self, sender):
        self.filter = sender
        self.sel = max(len(self.visible()) - 1, 0)
        self.post_top = 0

    def toggle_expand(self):
        post = self.selected_post()
        if post:
            self.expanded ^= {post.seq}

    @property
    def message_visible(self):
        return self.show_message or self.zoom or self.show_log

    def cycle_focus(self):
        panes = [p for p in PANES if p != "message" or self.message_visible]
        index = panes.index(self.focus) if self.focus in panes else -1
        self.focus = panes[(index + 1) % len(panes)]

    def toggle_message(self):
        if self.show_log:
            self.close_log()
            return
        self.show_message = not self.show_message
        self.zoom = self.zoom and self.show_message
        if not self.show_message and self.focus == "message":
            self.focus = "posts"

    def toggle_zoom(self):
        self.zoom = not self.zoom
        if self.zoom:
            self.show_message, self.focus = True, "message"

    def escape(self):
        if self.show_log:
            self.close_log()
        elif self.zoom:
            self.zoom = False
        else:
            self.set_filter(None)


def token_text(match, state):
    code, bold, mention, path = match.groups()
    if code is not None:
        return Text(code, style=BLUE)
    if bold is not None:
        return Text(bold, style="bold")
    if mention is not None:
        name = mention[1:]
        return Text(mention, style=state.color(name) if state.knows(name) else DIM)
    return Text(path, style=BLUE)


def style_inline(line, state):
    out, pos = Text(), 0
    for match in TOKEN.finditer(line):
        out.append(line[pos:match.start()])
        out.append_text(token_text(match, state))
        pos = match.end()
    out.append(line[pos:])
    return out


def line_parts(raw, state, code):
    if code:
        return "", Text(raw, style=BLUE)
    bullet = BULLET.match(raw)
    prefix, rest = bullet.groups() if bullet else ("", raw)
    if HEADING.match(rest):
        return prefix, Text(HEADING.sub("", rest), style="bold")
    return prefix, style_inline(rest, state)


def wrap_line(raw, width, state, code):
    prefix, text = line_parts(raw, state, code)
    wrapped = text.wrap(CONSOLE, max(width - len(prefix), 8)) or [Text("")]
    out = []
    for index, piece in enumerate(wrapped):
        lead = prefix if index == 0 else " " * len(prefix)
        out.append(Text(lead).append_text(piece))
    return out


def body_lines(body, width, state):
    out, fenced = [], False
    for raw in body.split("\n"):
        if raw.strip().startswith("```"):
            fenced = not fenced
            out.extend(Text(raw, style=DIM).wrap(CONSOLE, max(width, 8)))
            continue
        out.extend(wrap_line(raw, width, state, fenced))
    return out


def header_text(state):
    text = Text()
    text.append("Swarm board ", style="bold")
    text.append(f'"{state.title or state.room}"', style="bold #c9d1d9")
    text.append(f" · {plural(len(state.posts), 'post')} · ", style=DIM)
    if state.finished():
        text.append("✓ finished", style=f"bold {GREEN}").append(" · ", style=DIM)
    text.append(f"{plural(len(state.roster()), 'member')} · ", style=DIM)
    usage = usage_text(*state.total_usage())
    if usage:
        text.append(usage + " · ", style=DIM)
    on = state.follow
    text.append(f"[follow: {'on' if on else 'off'}]", style=GREEN if on else DIM)
    return text


def label_text(label, focused):
    return Text(label, style=f"bold {BLUE}" if focused else DIM)


def roster_label(state):
    label = f"Roster · {plural(len(state.active()), 'member')}, {state.waiting_count()} waiting"
    if state.common_model():
        label += f" · all on {state.common_model()}"
    return label_text(label, state.focus == "roster")


def status_text(state, name, now):
    status = state.status(name)
    if status in ("gone", "retired"):
        return Text(f"○ {status}", style=DIM)
    age = since(state.info(name).get("state_at"), now)
    suffix = f" {age}" if age else ""
    if status == "working":
        return Text("● working", style=GREEN).append(suffix, style=DIM)
    return Text("○ waiting" + suffix, style=DIM)


def name_line(state, name, colw, show_glyph=True):
    color = state.color(name)
    tokens, cost, _ = state.member_usage(name)
    right = usage_text(tokens, cost)
    crown = " ♛" if state.is_coordinator(name) else ""
    lead_w = 2 if show_glyph else 0
    room = colw - lead_w - len(crown) - (len(right) + 1 if right else 0)
    head = Text()
    if show_glyph:
        head.append(state.glyph(name) + " ", style=color)
    head.append(ellipsize(name, max(room, 4)), style=Style(color=color, bold=True))
    head.append(crown, style=GOLD)
    if right:
        gap = max(colw - head.cell_len - len(right), 1)
        head.append(" " * gap).append(right, style=DIM)
    return head


def runtime_text(info):
    parts = [p for p in (info.get("harness"), info.get("model")) if p]
    return " · ".join(parts) or "-"


def card_lines(state, name, colw, selected, now):
    info = state.info(name)
    show_avatar = colw >= AVATAR_MIN_COLW
    text_w = colw - AVATAR_WIDTH - 1 if show_avatar else colw
    pad = "" if show_avatar else "  "
    avail = text_w if show_avatar else max(text_w - 3, 4)
    status = Text(pad).append_text(status_text(state, name, now))
    runtime = Text(pad + ellipsize(runtime_text(info), avail), style=DIM)
    doing = Text(pad + ellipsize(state.doing(name), avail), style=DOING_STYLE)
    body = [
        fit(line, text_w)
        for line in (name_line(state, name, text_w, show_glyph=not show_avatar), status, runtime, doing)
    ]
    if show_avatar:
        sprite = avatar_lines(state, name, state.frame)
        lines = [fit(Text().append_text(sprite[i]).append(" ").append_text(body[i]), colw) for i in range(CARD_LINES)]
    else:
        lines = body
    if selected:
        bg = SELECTED_BG if state.focus == "roster" else UNFOCUSED_BG
        for line in lines:
            line.stylize(bg)
    return lines


def card_width(width):
    return max((width - (CARD_COLUMNS - 1) * 2) // CARD_COLUMNS, 8)


def card_at(state, width, x, y):
    """The member whose card covers content cell (x, y) of the roster pane, or None for gaps and blank space."""
    colw = card_width(width)
    col, dx = divmod(x, colw + 2)
    row, dy = divmod(y, CARD_LINES + 1)
    if col >= CARD_COLUMNS or dx >= colw or row >= ROSTER_ROWS or dy >= CARD_LINES:
        return None
    names = state.active()
    index = (state.roster_top + row) * CARD_COLUMNS + col
    return names[index] if index < len(names) else None


def card_rows(state, width, now):
    names = state.active()
    colw = card_width(width)
    selected = state.member_name()
    cells = [card_lines(state, n, colw, n == selected, now) for n in names]
    blank = [Text(" " * colw)] * CARD_LINES
    return [cells[i:i + CARD_COLUMNS] for i in range(0, len(cells), CARD_COLUMNS)], blank, colw


def roster_view(state, width, now):
    rows, blank, _ = card_rows(state, width, now)
    if not rows and not state.retired():
        return Text("no members yet", style=DIM)
    selected = state.member_name()
    active = state.active()
    index = (active.index(selected) if selected in active else 0) // CARD_COLUMNS
    state.roster_top = keep_visible(state.roster_top, index, index, ROSTER_ROWS)
    lines = []
    for row in rows[state.roster_top:state.roster_top + ROSTER_ROWS]:
        row = row + [blank] * (CARD_COLUMNS - len(row))
        lines.extend(Text("  ").join(cell[i] for cell in row) for i in range(CARD_LINES))
        lines.append(Text(""))
    lines.extend(retired_lines(state, width))
    return Text("\n").join(lines[:-1] if not state.retired() else lines)


def retired_lines(state, width):
    names = state.retired()
    if not names:
        return []
    line = f"Retired ({len(names)}): " + ", ".join(names)
    return [Text(ellipsize(line, width), style=DIM)]


def detail_field(label, value):
    return Text(f"{label:<14}", style=DIM).append(value or "-")


def detail_view(state, width, now):
    name = state.member_name()
    if name is None:
        return Text("")
    info = state.info(name)
    head = Text()
    head.append(state.glyph(name) + " ", style=state.color(name))
    head.append(name, style=Style(color=state.color(name), bold=True))
    lines = [
        head,
        detail_field("role", info.get("role")),
        detail_field("harness", info.get("harness")),
        detail_field("model", info.get("model")),
        detail_field("conversation", info.get("conversation")),
    ]
    lines.extend(wrapped_field("workspace", info.get("workspace"), width))
    lines.append(detail_field("state", state_line(state, name, now)))
    lines.extend(wrapped_field("doing", state.doing(name), width))
    lines.extend(optional_fields(info))
    lines.append(detail_field("last post", last_post_text(state, name, now)))
    return Text("\n").join(lines)


def state_line(state, name, now):
    status = state.status(name)
    age = since(state.info(name).get("state_at"), now)
    return f"{status} since {age} ago" if age and status not in ("gone", "retired") else status


def optional_fields(info):
    lines = []
    if info.get("groups"):
        lines.append(detail_field("groups", " ".join("@" + g for g in info["groups"])))
    usage = usage_detail(info)
    if usage:
        lines.append(detail_field("usage", usage))
    return lines


def usage_detail(info):
    parts = []
    if info.get("tokens_in") is not None or info.get("tokens_out") is not None:
        parts.append(
            f"in {short_count(to_number(info.get('tokens_in')))} / "
            f"out {short_count(to_number(info.get('tokens_out')))}"
        )
    if to_number(info.get("tokens_cached")):
        parts.append(f"cached {short_count(to_number(info.get('tokens_cached')))}")
    if info.get("turns") is not None:
        parts.append(plural(to_number(info.get("turns")), "turn"))
    if info.get("cost_usd") is not None:
        parts.append(f"${to_number(info.get('cost_usd'), float):.2f}")
    return " · ".join(parts)


def wrapped_field(label, value, width):
    pieces = textwrap.wrap(value or "-", max(width - 14, 10), break_long_words=True, break_on_hyphens=False) or ["-"]
    return [detail_field(label if i == 0 else "", piece) for i, piece in enumerate(pieces)]


def last_post_text(state, name, now):
    post = state.last_post_of(name)
    return f"#{post.seq}, {ago(post.ms, now)}" if post else "none"


def posts_label(state):
    label = f"Posts · {len(state.posts)} total"
    if state.filter is not None:
        label += f" · sender: {state.filter} ({len(state.visible())})"
    if state.post_rows:
        label += " · rows {}-{} of {}".format(*state.post_rows)
    return label_text(label, state.focus == "posts")


def post_row(post, state, widths, selected, focused):
    seq_w, cell_w, stamp_w, width, now = widths
    row = Text()
    row.append("> " if selected else "  ", style=("bold " + GREEN) if focused else DIM)
    row.append(f"#{post.seq:<{seq_w}}  ", style=DIM)
    row.append(f"{fmt_when(post.ms, now):<{stamp_w}}  ", style=DIM)
    row.append_text(fit(sender_cell(post, state, cell_w), cell_w))
    row.append("  ")
    tag = kind_tag(post.kind)
    if tag:
        row.append_text(tag).append(" ")
    first = post.body.strip().split("\n", 1)[0]
    row.append_text(style_inline(first, state))
    fit(row, width)
    if selected:
        row.stylize(SELECTED_BG if focused else UNFOCUSED_BG)
    return row


def natural_cell_width(post):
    return len(post.sender) + (3 + len(post.to) if post.to != "all" else 0)


def sender_cell(post, state, cell_w):
    cell = Text()
    sender = post.sender
    if post.to == "all":
        return cell.append(ellipsize(sender, cell_w), style=Style(color=state.color(sender), bold=True))
    room = cell_w - 3 - len(sender)
    if room < MIN_RECIPIENT_WIDTH:
        sender = ellipsize(sender, max(cell_w - 3 - MIN_RECIPIENT_WIDTH, 1))
        room = cell_w - 3 - len(sender)
    cell.append(sender, style=Style(color=state.color(post.sender), bold=True))
    return cell.append(" → ", style=DIM).append(ellipsize(post.to, room), style=DIM)


def expanded_lines(post, state, width):
    lines = body_lines(post.body.strip(), max(width - 6, 10), state)
    shown = [Text("      ").append_text(line) for line in lines[:EXPANDED_CAP]]
    if len(lines) > EXPANDED_CAP:
        shown.append(Text("      …", style=DIM))
    return shown


def posts_view(state, width, height, now):
    visible = state.visible()
    state.post_rows, state.line_seqs = None, []
    if not visible:
        return Text("no posts yet", style=DIM)
    stamp_w = max(len(fmt_when(p.ms, now)) for p in state.posts)
    cell_w = min(max(natural_cell_width(p) for p in state.posts), SENDER_CELL_WIDTH)
    widths = (len(str(len(state.posts))), cell_w, stamp_w, width, now)
    focused = state.focus == "posts"
    lines, starts = [], []
    for index, post in enumerate(visible):
        starts.append(len(lines))
        lines.append(post_row(post, state, widths, index == state.sel, focused))
        if post.seq in state.expanded:
            lines.extend(expanded_lines(post, state, width))
        state.line_seqs.extend([post.seq] * (len(lines) - starts[-1]))
    first = starts[state.sel]
    last = (starts[state.sel + 1] if state.sel + 1 < len(starts) else len(lines)) - 1
    state.post_top = keep_visible(state.post_top, first, last, height)
    shown = [i + 1 for i, start in enumerate(starts) if state.post_top <= start < state.post_top + height]
    if len(shown) < len(visible):
        state.post_rows = (shown[0], shown[-1], len(visible))
    return Text("\n").join(lines[state.post_top:state.post_top + height])


def post_index_at(state, y):
    """Index into the visible posts of the one drawn on content row `y` of the posts pane."""
    line = state.post_top + y
    if not 0 <= line < len(state.line_seqs):
        return None
    seq = state.line_seqs[line]
    return next((i for i, post in enumerate(state.visible()) if post.seq == seq), None)


def read_log_tail(path):
    if not os.path.isfile(path):
        raise FileNotFoundError(path)
    with open(path, "rb") as handle:
        start = max(handle.seek(0, os.SEEK_END) - LOG_TAIL_BYTES, 0)
        handle.seek(start)
        lines = handle.read(LOG_TAIL_BYTES).decode("utf-8", "replace").split("\n")
    if lines[-1] == "":
        lines.pop()
    if start and len(lines) > 1:
        del lines[0]
    return [line.removesuffix("\r") for line in lines[-LOG_TAIL_LINES:]]


def log_rows(path, name, width):
    """The log tail as escaped, wrapped rows. A name that is not a plain id never reaches the filesystem."""
    try:
        store.ident(name)
        lines = read_log_tail(path)
    except (argparse.ArgumentTypeError, FileNotFoundError):
        lines = [f"(no log file at {path})"]
    except OSError as exc:
        lines = [f"(cannot read log file at {path}: {exc.strerror})"]
    return [row for line in lines for row in Text(clean(line)).wrap(CONSOLE, width) or [Text("")]]


def log_view(state, width, height):
    """Pinned to the tail until scrolled up; the lines then hold still so new output cannot move them."""
    name = state.member_name()
    if name is None:
        return label_text("Log · none", state.focus == "message"), Text("")
    path = os.path.join(store.state_root(), state.room, f"{name}.log")
    if name != state.log_name:
        state.log_name, state.log_top = name, None
    if state.log_top is None or state.log_width != width:
        state.log_lines, state.log_width = log_rows(path, name, width), width
    total = len(state.log_lines)
    state.log_max = max(total - height, 0)
    if state.log_top is not None and state.log_top >= state.log_max:
        state.log_top = None
    top = state.log_max if state.log_top is None else state.log_top
    label = label_text(f"Log · {name}", state.focus == "message")
    label.append(f" · {clean(path)} · lines {min(top + 1, total)}-{min(top + height, total)} of {total}")
    return label, Text("\n").join(state.log_lines[top:top + height])


def message_view(state, width, height, now):
    post = state.selected_post()
    if post is None:
        return label_text("Message · none", state.focus == "message"), Text("")
    if state.msg_seq != post.seq:
        state.msg_seq, state.msg_top = post.seq, 0
    lines = body_lines(post.body.strip(), width, state)
    state.msg_top = clamp(state.msg_top, 0, max(len(lines) - height, 0))
    top = state.msg_top
    span = f"{min(top + 1, len(lines))}-{min(top + height, len(lines))}"
    label = label_text(f"Message · #{post.seq} {post.sender}", state.focus == "message")
    tag = kind_tag(post.kind)
    if tag:
        label.append(" ").append_text(tag)
    label.append(f" · {hhmmss(post.ms)} · lines {span} of {len(lines)}")
    return label, Text("\n").join(lines[top:top + height])


def footer_status(state, now):
    if state.pending_finish:
        return Text(
            f'Finish swarm "{clean(state.room)}" (--kind done to all)? press y to confirm, esc cancels',
            style=f"bold {GOLD}",
        )
    if state.notice and now < state.notice_until:
        return state.notice
    if state.error:
        return Text(state.error, style=ERROR_STYLE)
    post = state.selected_post()
    if post is None:
        return Text("No posts", style=DIM)
    n = state.sel + 1
    when = f"seq #{post.seq} by {post.sender} at {hhmmss(post.ms)}, {ago(post.ms, now)}"
    return Text(f"Post {n}/{len(state.visible())} ({when})", style="#c9d1d9")


def parse_compose(line, default):
    """(recipients, body) for a compose line. Raises ArgumentTypeError when it cannot be sent as typed."""
    text = line.strip()
    head = text.split(None, 1)[0]
    if head == "/to":
        parts = text.split(None, 2)[1:]
    elif head.startswith("@"):
        parts = text.split(None, 1)
    else:
        return default, text
    if len(parts) < 2:
        raise argparse.ArgumentTypeError("add a recipient and a message: @name message, or /to a,b message")
    names = ",".join("all" if name == "@all" else name for name in parts[0].split(","))
    return store.recipients(names), parts[1]


class Pane(Static, can_focus=True):
    def on_resize(self, event):
        self.app.render_all()

    def on_click(self, event):
        self.app.click_pane(self.id, event.get_content_offset(self))

    def on_mouse_scroll_up(self, event):
        event.stop()
        self.app.wheel_pane(self.id, -1)

    def on_mouse_scroll_down(self, event):
        event.stop()
        self.app.wheel_pane(self.id, 1)


class SwarmBoard(App):
    CSS = """
    Screen { background: #0b0e14; color: #c9d1d9; }
    Rule { margin: 0; color: #21262d; }
    #top { height: auto; }
    #roster, #detail { height: auto; }
    #detail { padding-top: 1; padding-left: 1; }
    #roster, #posts, #message { border-left: outer #21262d; }
    #roster.active-pane, #posts.active-pane, #message.active-pane { border-left: outer #58a6ff; }
    #posts-sec { height: 2fr; }
    #msg-sec { height: 3fr; }
    #posts, #message { height: 1fr; }
    #bottom { dock: bottom; height: auto; }
    #compose { height: 1; }
    #compose-to { width: auto; padding-left: 1; }
    #compose-input { width: 1fr; height: 1; border: none; padding: 0; }
    #foot { height: 3; }
    Pane:focus { background: #0b0e14; }
    """
    BINDINGS = [
        Binding("tab", "next_pane", "switch pane", priority=True),
        Binding("shift+tab", "toggle_recipient", "recipient", priority=True, show=False),
        Binding("left,h", "move_horiz(-1)", "left", show=False),
        Binding("right", "move_horiz(1)", "right", show=False),
        Binding("up,k", "move(-1)", "up", show=False),
        Binding("down,j", "move(1)", "down", show=False),
        Binding("pageup", "page(-1)", "page up", show=False),
        Binding("pagedown", "page(1)", "page down", show=False),
        Binding("enter", "toggle_message", "hide message"),
        Binding("space", "expand", "expand"),
        Binding("p", "filter", "filter"),
        Binding("o", "open_workspace", "open"),
        Binding("f", "toggle_follow", "follow"),
        Binding("z", "zoom", "zoom"),
        Binding("escape", "back", "back"),
        Binding("m", "compose", "msg"),
        Binding("l", "log", "log"),
        Binding("d", "finish", "done"),
        Binding("b", "picker", "rooms"),
        Binding("q", "leave", "quit"),
    ]

    def __init__(self, r, room, interval=1.0, opener=None):
        super().__init__()
        self.r = r
        self.state = BoardState(room)
        self.interval = interval
        self.opener = opener or open_path
        self.polling = False

    def compose(self):
        with Vertical(id="top"):
            yield Static(id="header")
            yield Rule()
            yield Static(id="roster-label")
            yield Pane(id="roster")
            yield Static(id="detail")
        with Vertical(id="posts-sec"):
            yield Rule()
            yield Static(id="posts-label")
            yield Pane(id="posts")
        with Vertical(id="msg-sec"):
            yield Rule()
            yield Static(id="message-label")
            yield Pane(id="message")
        with Vertical(id="bottom"):
            with Horizontal(id="compose"):
                yield Static(id="compose-to")
                yield Input(id="compose-input", placeholder=COMPOSE_HINT, select_on_focus=False)
            with Vertical(id="foot"):
                yield Rule()
                yield Static(id="status")
                yield Static(Text(HINTS, style=DIM), id="hints")

    def on_mount(self):
        self.query_one("#compose").display = False
        self.query_one("#posts").focus()
        if self.interval:
            self.set_interval(self.interval, self.poll)
            self.set_interval(ANIM_INTERVAL, self.tick_animation)
        self.poll()

    def tick_animation(self):
        if not any(self.state.status(n) in ("working", "waiting") for n in self.state.active()):
            return
        self.state.frame = (self.state.frame + 1) % 8
        try:
            st, now = self.state, time.time()
            self.query_one("#roster", Pane).update(roster_view(st, self.pane_size("roster")[0], now))
        except NoMatches:
            return

    def on_resize(self, event):
        self.render_all()

    def poll(self):
        if self.polling:
            return
        self.polling = True
        self.run_worker(self.poll_worker, thread=True, group="poll")

    def poll_worker(self):
        try:
            snap = fetch(self.r, self.state.room, self.state.last_id, len(self.state.posts))
        except redis.RedisError as exc:
            self.call_from_thread(self.apply_error, f"redis: {exc}")
        else:
            self.call_from_thread(self.apply, snap)

    def apply(self, snap):
        self.polling = False
        self.state.ingest(snap)
        self.render_all()

    def apply_error(self, message):
        self.polling = False
        self.state.error = message
        self.render_all()

    def pane_size(self, name):
        size = self.query_one(f"#{name}").size
        return size.width or self.size.width or 80, size.height or PAGE_FALLBACK

    def render_all(self):
        try:
            self.render_panes()
        except NoMatches:
            return

    def render_panes(self):
        st, now = self.state, time.time()
        self.apply_layout()
        self.query_one("#header", Static).update(header_text(st))
        self.query_one("#roster-label", Static).update(roster_label(st))
        self.query_one("#roster", Pane).update(roster_view(st, self.pane_size("roster")[0], now))
        self.query_one("#detail", Static).update(detail_view(st, self.pane_size("detail")[0], now))
        width, height = self.pane_size("posts")
        self.query_one("#posts", Pane).update(posts_view(st, width, height, now))
        self.query_one("#posts-label", Static).update(posts_label(st))
        self.render_message(now)
        self.query_one("#status", Static).update(footer_status(st, now))

    def render_message(self, now):
        width, height = self.pane_size("message")
        if self.state.show_log:
            label, body = log_view(self.state, width, height)
        else:
            label, body = message_view(self.state, width, height, now)
        self.query_one("#message-label", Static).update(label)
        self.query_one("#message", Pane).update(body)

    def apply_layout(self):
        st = self.state
        self.query_one("#top").display = not st.zoom
        self.query_one("#posts-sec").display = not st.zoom
        self.query_one("#msg-sec").display = st.message_visible
        self.query_one("#compose").display = st.composing
        self.query_one("#compose-to", Static).update(Text(f"to {st.compose_to} › ", style=f"bold {BLUE}"))
        for pane_id in PANES:
            self.query_one(f"#{pane_id}").set_class(pane_id == st.focus, "active-pane")
        self.query_one("#compose-input" if st.composing else f"#{st.focus}").focus()

    def refresh_board(self):
        self.render_all()
        self.call_after_refresh(self.render_all)

    async def on_event(self, event):
        """A key answers a pending finish before any binding, priority ones included, can act on it."""
        if isinstance(event, events.Key) and not event.is_forwarded:
            if self.state.pending_finish:
                self.answer_finish(event.key == "y")
                return
            self.state.notice = None
        await super().on_event(event)

    def on_mouse_down(self, event):
        if self.state.pending_finish:
            self.answer_finish(False)

    def on_click(self, event):
        if self.state.composing:
            self.set_focus(self.query_one("#compose-input"))

    def check_action(self, action, parameters):
        if action == "toggle_recipient":
            return self.state.composing
        return not (self.state.composing and action in BLOCKED_WHILE_COMPOSING)

    def click_pane(self, pane, offset):
        if offset is not None:
            self.select_at(pane, offset)
        self.state.focus = pane
        self.refresh_board()

    def select_at(self, pane, offset):
        st = self.state
        if pane == "roster":
            st.member = card_at(st, self.pane_size(pane)[0], offset.x, offset.y) or st.member
        elif pane == "posts":
            index = post_index_at(st, offset.y)
            if index is not None:
                st.select_post(index)

    def wheel_pane(self, pane, direction):
        if pane == "roster":
            self.state.move_member_grid(0, direction)
        elif pane == "posts":
            self.state.move_post(direction)
        else:
            self.state.scroll_message(direction * WHEEL_STEP)
        self.render_all()

    def say(self, message, style="#c9d1d9"):
        self.state.notice = Text(clean(message), style=style)
        self.state.notice_until = time.time() + NOTICE_SECONDS
        self.render_all()

    def publish(self, fields):
        try:
            self.r.xadd(store.k_stream(self.state.room), fields)
        except redis.RedisError as exc:
            self.say(f"redis: {exc}", ERROR_STYLE)
            return False
        return True

    def action_compose(self):
        self.state.open_compose()
        self.render_all()
        self.set_focus(self.query_one("#compose-input"))
        self.call_after_refresh(self.render_all)

    def action_toggle_recipient(self):
        self.state.toggle_recipient()
        self.render_all()

    def close_compose(self):
        self.state.close_compose()
        self.query_one("#compose-input", Input).value = ""
        self.render_all()
        self.set_focus(self.query_one(f"#{self.state.focus}"))
        self.call_after_refresh(self.render_all)

    def on_input_submitted(self, event):
        if not event.value.strip():
            self.close_compose()
            return
        try:
            to, body = parse_compose(event.value, self.state.compose_to)
        except argparse.ArgumentTypeError as exc:
            self.say(str(exc), ERROR_STYLE)
            return
        if not self.publish(store.make_fields(OPERATOR, to, body)):
            return
        note = self.announce(to)
        self.close_compose()
        self.say(note)
        self.poll()

    def announce(self, to):
        """Mark the operator present and word the confirmation. The post is already in, so errors here are moot."""
        note = f"sent to {to}"
        with contextlib.suppress(redis.RedisError):
            store.touch_presence(self.r, self.state.room, OPERATOR)
            unknown = store.unknown_mentions(self.r, self.state.room, to)
            if unknown:
                note += f" (no member or group named {', '.join(unknown)})"
        return note

    def action_finish(self):
        self.state.pending_finish = True
        self.render_all()

    def answer_finish(self, confirmed):
        self.state.pending_finish = False
        if not confirmed:
            self.say("cancelled")
            return
        sender = self.state.coordinator_name()
        if self.publish(store.make_fields(sender, "all", "Finished from board.", kind="done")):
            self.say(f"posted --kind done to all as {sender}")
            self.poll()

    def action_log(self):
        self.state.toggle_log()
        self.refresh_board()

    def action_leave(self):
        self.exit()

    def action_next_pane(self):
        if self.state.composing:
            self.action_toggle_recipient()
            return
        self.state.cycle_focus()
        self.refresh_board()

    def action_move_horiz(self, delta):
        if self.state.focus == "roster":
            self.state.move_member_grid(delta, 0)
            self.render_all()

    def action_move(self, delta):
        st = self.state
        if st.focus == "roster":
            st.move_member_grid(0, delta)
        elif st.focus == "posts":
            st.move_post(delta)
        else:
            st.scroll_message(delta)
        self.render_all()

    def action_page(self, direction):
        if self.state.focus == "message":
            self.state.scroll_message(direction * self.pane_size("message")[1])
            self.render_all()

    def action_toggle_message(self):
        self.state.toggle_message()
        self.refresh_board()

    def action_expand(self):
        self.state.toggle_expand()
        self.render_all()

    def action_filter(self):
        self.state.cycle_filter()
        self.render_all()

    def action_toggle_follow(self):
        self.state.toggle_follow()
        self.render_all()

    def action_zoom(self):
        self.state.toggle_zoom()
        self.refresh_board()

    def action_back(self):
        if self.state.composing:
            self.close_compose()
            return
        self.state.escape()
        self.refresh_board()

    def action_picker(self):
        self.exit(PICKER)

    def action_open_workspace(self):
        info = self.state.info(self.state.member_name() or "")
        path = workspace_path(info.get("workspace"))
        if path is None:
            self.notify("no workspace directory to open", severity="warning")
            return
        self.opener(path)


def workspace_path(value):
    if not value or not os.path.isabs(value) or not os.path.isdir(value):
        return None
    return os.path.realpath(value)


def open_path(path):
    opener = "open" if sys.platform == "darwin" else "xdg-open"
    subprocess.Popen([opener, path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


DONE = {"archive": "archived", "clear": "cleared"}
TITLE_WIDTH = 40
PICKER_KEYS = {"a", "x", "y", "r", "q", "escape", "up", "down", "enter"}


def cli_command(url, *words):
    prefix = "swarm" if url == store.default_url() else f"swarm --url {shlex.quote(url)}"
    return " ".join([prefix, *words])


def room_cells(row, now):
    last = row["last_activity_ms"]
    present = Text(str(row["present"]), style=GREEN if row["present"] else DIM, justify="right")
    title = ellipsize(store.terminal_line(row["title"]), TITLE_WIDTH)
    return (
        Text(clean(row["room"]), style="bold"),
        Text(title),
        Text(str(row["messages"]), justify="right"),
        Text(ago(last, now) if last else "-"),
        present,
    )


def picker_header(rows, notes):
    text = Text()
    text.append("Swarm rooms ", style="bold").append(f"· {plural(len(rows), 'room')}", style=DIM)
    if notes:
        text.append(" · " + notes, style=GOLD)
    return text


class RoomPicker(App):
    """Live rooms, newest activity first. Archive, clear and the open-time sweep are the only writes."""

    CSS = """
    Screen { background: #0b0e14; color: #c9d1d9; }
    Rule { margin: 0; color: #21262d; }
    DataTable { height: 1fr; background: #0b0e14; }
    #foot { dock: bottom; height: 3; }
    """
    BINDINGS = [
        Binding("a", "archive", "archive"),
        Binding("x", "clear", "clear"),
        Binding("y", "confirm", "confirm", show=False),
        Binding("r", "refresh_rooms", "refresh"),
        Binding("escape", "cancel", "cancel", show=False),
        Binding("q", "quit", "quit"),
    ]

    def __init__(self, r, idle_hours=0, retain_seconds=7 * 86400, interval=2.0, url=None):
        super().__init__()
        self.r = r
        self.idle_hours = idle_hours
        self.retain_seconds = retain_seconds
        self.interval = interval
        self.url = url or store.default_url()
        self.rooms = []
        self.pending = None
        self.notes = ""
        self.message = ""
        self.error = ""
        self.sweep_due = bool(idle_hours)
        self.polling = False

    def compose(self):
        yield Static(id="header")
        yield DataTable(id="rooms", cursor_type="row", zebra_stripes=False)
        with Vertical(id="foot"):
            yield Rule()
            yield Static(id="status")
            yield Static(Text(PICKER_HINTS, style=DIM), id="hints")

    def on_mount(self):
        table = self.query_one(DataTable)
        table.add_columns("Room", "Title", "Messages", "Last activity", "Present")
        table.focus()
        if self.interval:
            self.set_interval(self.interval, self.poll)
        self.poll()

    def poll(self):
        if self.polling:
            return
        self.polling = True
        self.run_worker(self.poll_worker, thread=True, group="poll")

    def poll_worker(self):
        swept = []
        try:
            self.sweep(swept)
            rows = store.list_rooms(self.r)
        except redis.RedisError as exc:
            self.call_from_thread(self.apply_error, f"redis: {exc}", swept)
        else:
            self.call_from_thread(self.apply, rows, swept)

    def sweep(self, swept):
        if self.sweep_due:
            store.sweep_idle(self.r, self.idle_hours, self.retain_seconds, archived=swept)
            self.sweep_due = False

    def note_swept(self, swept):
        if swept:
            names = ", ".join(clean(s["room"]) for s in swept)
            self.notes = f"auto-archived {plural(len(swept), 'idle room')}: {names}"

    def apply(self, rows, swept=()):
        self.polling = False
        self.error = ""
        self.note_swept(swept)
        keep = self.selected_name()
        self.rooms = rows
        self.render_rooms(keep)

    def apply_error(self, message, swept=()):
        self.polling = False
        self.error = message
        self.note_swept(swept)
        self.render_header()
        self.render_status()

    def render_rooms(self, keep):
        table, now = self.query_one(DataTable), time.time()
        table.clear()
        for row in self.rooms:
            table.add_row(*room_cells(row, now), key=row["room"])
        names = [row["room"] for row in self.rooms]
        if keep in names:
            table.move_cursor(row=names.index(keep))
        self.render_header()
        self.render_status()

    def render_header(self):
        self.query_one("#header", Static).update(picker_header(self.rooms, self.notes))

    def render_status(self):
        if self.pending:
            row = next((r for r in self.rooms if r["room"] == self.pending), None)
            count = row["messages"] if row else 0
            line = Text(
                f'Clear "{clean(self.pending)}" and its {plural(count, "message")} for good? '
                "press y to confirm, esc cancels",
                style=f"bold {GOLD}",
            )
        elif self.error:
            line = Text(self.error, style="bold #ff7b72")
        else:
            line = Text(self.message, style="#c9d1d9")
        self.query_one("#status", Static).update(line)

    def selected_name(self):
        table = self.query_one(DataTable)
        if not self.rooms or not 0 <= table.cursor_row < len(self.rooms):
            return None
        return self.rooms[table.cursor_row]["room"]

    def say(self, message):
        self.pending, self.message, self.notes = None, message, ""
        self.render_header()
        self.render_status()

    def on_data_table_row_selected(self, event):
        if self.drop_prompt():
            return
        self.exit(event.row_key.value)

    def on_key(self, event):
        if self.pending and event.key not in PICKER_KEYS:
            self.say("cancelled")

    def action_refresh_rooms(self):
        if self.drop_prompt():
            return
        self.say("")
        self.poll()

    def action_cancel(self):
        self.say("cancelled" if self.pending else "")

    def drop_prompt(self):
        was_pending = self.pending is not None
        if was_pending:
            self.say("cancelled")
        return was_pending

    def action_archive(self):
        name = self.selected_name()
        if self.drop_prompt() or name is None:
            return
        self.attempt(name, "archive", lambda: store.archive_room(self.r, name, self.retain_seconds))

    def action_clear(self):
        name = self.selected_name()
        if self.drop_prompt() or name is None:
            return
        self.pending = name
        self.render_status()

    def action_confirm(self):
        name, self.pending = self.pending, None
        if name is not None:
            self.attempt(name, "clear", lambda: store.clear_room(self.r, name))

    def attempt(self, name, verb, call):
        try:
            result = call()
        except redis.RedisError as exc:
            self.say(f"redis: {exc}")
            return
        self.finish(name, result, verb)

    def finish(self, name, result, verb):
        shown = clean(name)
        if result["status"] == "ok":
            undo = cli_command(self.url, "unarchive", result["archive_id"]) if verb == "archive" else ""
            self.say(f"{DONE[verb]} {shown}" + (f" as {result['archive_id']}; `{undo}` brings it back" if undo else ""))
        elif result["status"] == "present":
            force = cli_command(self.url, verb, "--room", name, "--force")
            self.say(f"{shown} has {result['present']} member(s) present; `{force}` does it anyway")
        else:
            self.say(f"{shown} is already gone")
        self.poll()


def browse(r, room, url=None):
    """Picker and boards take turns: picker exits with a room name, board exits with PICKER on `b`."""
    url = url or store.default_url()
    while True:
        if room is None:
            room = RoomPicker(r, store.archive_idle_hours(), store.archive_retain_seconds(), url=url).run()
            if room is None:
                return
        if SwarmBoard(r, room).run() != PICKER:
            return
        room = None


def run_board(args):
    try:
        r = store.connect(args.url)
    except redis.RedisError as exc:
        sys.stderr.write(f"swarm board: cannot reach Redis at {args.url}: {exc}\n")
        return 1
    room = args.room if getattr(args, "room_given", False) else None
    browse(r, room, args.url)
    return 0
