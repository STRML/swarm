"""Pilot tests for `swarm board` (SwarmBoard) against an in-memory fakeredis."""
import os
import time
import unittest

try:
    import fakeredis
    import textual  # noqa: F401
except ImportError:  # pragma: no cover
    fakeredis = None

from swarm import board, store

ROOM = "swarm-test"
SIZE = (120, 50)
BASE_MS = int(time.time() * 1000) - 600_000

POSTS = [
    ("coordinator", "all", "Plan for @w1 and @verifier\n- first step\n- second step"),
    ("w1", "coordinator", "Working on `src/swarm/board.py` now, **bold** claim"),
    ("verifier", "all", "Waiting for [bold red]markup[/] and \x1b[31mescapes"),
    ("w1", "all", "Done: " + "word " * 60),
]


def text_of(widget):
    return widget.content.plain


@unittest.skipIf(fakeredis is None, "fakeredis/textual not installed")
class SwarmBoardTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.r = fakeredis.FakeStrictRedis(decode_responses=True)
        self.r.hset(store.k_meta(ROOM), "title", "Epic 62")
        self.member("coordinator", role="coordinator", model="claude-opus", state="waiting")
        self.member(
            "w1", role="worker", model="claude-sonnet-5-5", state="working",
            workspace="/tmp/does-not-exist", conversation="sess-1",
        )
        self.member("verifier", role="verifier", model="gpt-5", state="waiting")
        for i, (sender, to, body) in enumerate(POSTS):
            self.post(i, sender, to, body)

    def member(self, name, **fields):
        self.r.hset(store.k_member(ROOM, name), mapping=fields)
        self.r.set(store.k_presence(ROOM, name), "now", ex=90)

    def post(self, index, sender, to, body, kind="msg"):
        self.r.xadd(
            store.k_stream(ROOM),
            {"from": sender, "to": to, "body": body, "kind": kind},
            id=f"{BASE_MS + index * 60_000}-0",
        )

    def make_app(self, interval=0, opener=None):
        return board.SwarmBoard(self.r, ROOM, interval=interval, opener=opener)

    async def load(self, app, pilot):
        app.apply(board.fetch(self.r, ROOM, app.state.last_id, len(app.state.posts)))
        await pilot.pause()

    async def test_header_roster_and_posts(self):
        app = self.make_app()
        async with app.run_test(size=SIZE) as pilot:
            await self.load(app, pilot)
            self.assertEqual(
                text_of(app.query_one("#header")),
                'Swarm board "Epic 62" · 4 posts · 3 members · [follow: on]',
            )
            self.assertEqual(text_of(app.query_one("#roster-label")), "Roster · 3 members, 2 waiting")
            roster = text_of(app.query_one("#roster"))
            self.assertIn("● working", roster)
            self.assertEqual(roster.count("○ waiting"), 2)
            self.assertIn("♛", roster)
            rows = text_of(app.query_one("#posts")).split("\n")
            self.assertEqual(len(rows), 4)
            self.assertTrue(rows[3].startswith("> #4"))

    async def test_header_totals_and_card_usage(self):
        self.r.hset(
            store.k_member(ROOM, "w1"),
            mapping={"tokens_in": 1_000_000, "tokens_out": 234_567, "cost_usd": 1.5, "tokens_cached": 9_000_000},
        )
        self.r.hset(
            store.k_member(ROOM, "coordinator"),
            mapping={"tokens_in": 100_000, "tokens_out": 28_000, "cost_usd": 0.42},
        )
        app = self.make_app()
        async with app.run_test(size=SIZE) as pilot:
            await self.load(app, pilot)
            self.assertEqual(
                text_of(app.query_one("#header")),
                'Swarm board "Epic 62" · 4 posts · 3 members · 1.4M tok · +9M cached · $1.92 · [follow: on]',
            )

    async def test_retired_members_and_finished_header(self):
        self.r.hset(store.k_member(ROOM, "w1"), "state", "retired")
        self.post(5, "coordinator", "all", "ship it", kind="done")
        app = self.make_app()
        async with app.run_test(size=SIZE) as pilot:
            await self.load(app, pilot)
            self.assertIn("Retired (1): w1", text_of(app.query_one("#roster")).split("\n")[-1])
            self.assertIn("✓ finished", text_of(app.query_one("#header")))

    async def test_untrusted_text_is_shown_literally(self):
        app = self.make_app()
        async with app.run_test(size=SIZE) as pilot:
            await self.load(app, pilot)
            await pilot.press("k")
            body = text_of(app.query_one("#message"))
            self.assertIn("[bold red]markup[/]", body)
            self.assertIn("\\x1b[31m", body)
            self.assertNotIn("\x1b", body)

    async def test_roster_grid_arrows_and_active_pane_indicator(self):
        for name in ("w2", "w3"):
            self.member(name, role="worker", model="claude-sonnet-5-5", state="working")
        app = self.make_app()
        async with app.run_test(size=SIZE) as pilot:
            await self.load(app, pilot)
            self.assertTrue(app.query_one("#posts").has_class("active-pane"))
            self.assertFalse(app.query_one("#roster").has_class("active-pane"))
            await pilot.press("tab", "tab")
            self.assertEqual(app.state.focus, "roster")
            self.assertTrue(app.query_one("#roster").has_class("active-pane"))
            self.assertFalse(app.query_one("#posts").has_class("active-pane"))
            self.assertEqual(app.state.active(), ["coordinator", "verifier", "w1", "w2", "w3"])
            app.state.member = "coordinator"
            await pilot.press("right")
            self.assertEqual(app.state.member_name(), "verifier")
            await pilot.press("down")
            self.assertEqual(app.state.member_name(), "w3")
            await pilot.press("left")
            self.assertEqual(app.state.member_name(), "w2")
            await pilot.press("up")
            self.assertEqual(app.state.member_name(), "coordinator")
            await pilot.press("right", "right", "down")
            self.assertEqual(app.state.member_name(), "w3")

    async def test_filter_expand_zoom_and_open(self):
        opened = []
        app = self.make_app(opener=opened.append)
        async with app.run_test(size=SIZE) as pilot:
            await self.load(app, pilot)
            await pilot.press("p")
            self.assertEqual(app.state.filter, "coordinator")
            await pilot.press("escape")
            self.assertIsNone(app.state.filter)
            await pilot.press("k", "k", "k", "space")
            self.assertIn("      - first step", text_of(app.query_one("#posts")).split("\n"))
            await pilot.press("z")
            self.assertFalse(app.query_one("#top").display)
            await pilot.press("escape")
            self.assertTrue(app.query_one("#top").display)
            self.r.hset(store.k_member(ROOM, "w1"), "workspace", os.path.realpath(os.path.dirname(__file__)))
            await self.load(app, pilot)
            app.state.member = "w1"
            await pilot.press("o")
            self.assertEqual(opened, [os.path.realpath(os.path.dirname(__file__))])


class PureHelperTests(unittest.TestCase):
    @unittest.skipIf(fakeredis is None, "fakeredis/textual not installed")
    def test_vendor_detection_and_avatar_cell_widths(self):
        from rich.cells import cell_len
        cases = [
            ({"model": "gemini-4-neon-high", "harness": "agy"}, "gemini"),
            ({"model": "claude-sonnet-5-5", "harness": "claude"}, "claude"),
            ({"model": "gpt-6.2-sol", "harness": "codex"}, "codex"),
            ({"model": "deepseek/deepseek-flash", "harness": "omp"}, "deepseek"),
            ({"model": "glm-5.3-flash", "harness": "omp"}, "omp"),
            ({"model": "unknown-model", "harness": "cmd"}, "generic"),
        ]
        for info, expected in cases:
            with self.subTest(info=info):
                self.assertEqual(board.vendor_of(info), expected)
        for vendor, spec in board.AVATARS.items():
            for frame in (*spec["working"], *spec["waiting"], spec["dim"]):
                self.assertEqual(len(frame), board.CARD_LINES, f"{vendor} frame height")
                for row in frame:
                    self.assertEqual(cell_len(row), board.AVATAR_WIDTH, f"{vendor} row {row!r} width")

    @unittest.skipIf(fakeredis is None, "fakeredis/textual not installed")
    def test_avatar_animation_and_distinct_member_tints(self):
        st = board.BoardState("r")
        st.members = {
            "bee": {"model": "gemini-4-hydrogen-high", "harness": "agy", "state": "working", "present": True},
            "clawd-1": {"model": "claude-sonnet-5-5", "harness": "claude", "state": "waiting", "present": True},
            "clawd-2": {"model": "claude-sonnet-5-5", "harness": "claude", "state": "working", "present": True},
            "whale": {"model": "deepseek/deepseek-flash", "harness": "omp", "state": "working", "present": True},
            "codex": {"model": "gpt-6.2-sol", "harness": "codex", "state": "working", "present": True},
            "gone-w": {"model": "claude-opus-5-5", "harness": "claude", "state": "working", "present": False},
        }
        st.assign_colors()
        self.assertGreaterEqual(len({tuple(t.plain for t in board.avatar_lines(st, "bee", f)) for f in range(4)}), 3)
        self.assertGreaterEqual(len({tuple(t.plain for t in board.avatar_lines(st, "whale", f)) for f in range(4)}), 3)
        self.assertGreaterEqual(len({tuple(t.plain for t in board.avatar_lines(st, "codex", f)) for f in range(4)}), 3)
        self.assertGreaterEqual(len({tuple(t.plain for t in board.avatar_lines(st, "clawd-1", f)) for f in range(8)}), 3)
        self.assertEqual(len({tuple(t.plain for t in board.avatar_lines(st, "gone-w", f)) for f in range(8)}), 1)
        self.assertNotEqual(
            board.avatar_color(st, "clawd-1", board.AVATARS["claude"]),
            board.avatar_color(st, "clawd-2", board.AVATARS["claude"]),
        )


if __name__ == "__main__":
    unittest.main()
