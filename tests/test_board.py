"""Pilot tests for `swarm board` (SwarmBoard) against an in-memory fakeredis."""
import os
import tempfile
import time
import unittest
from unittest import mock

try:
    import fakeredis
    import redis
    from textual import events
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


async def settle(pilot, predicate, timeout=5):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return
        await pilot.pause(0.05)
    raise AssertionError("timed out waiting")


class BoardCase(unittest.IsolatedAsyncioTestCase):
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

    def stream(self):
        return [fields for _, fields in self.r.xrange(store.k_stream(ROOM))]


@unittest.skipIf(fakeredis is None, "fakeredis/textual not installed")
class SwarmBoardTests(BoardCase):
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


async def type_text(pilot, text):
    await pilot.press(*text)
    await pilot.pause()


def status_of(app):
    return text_of(app.query_one("#status"))


@unittest.skipIf(fakeredis is None, "fakeredis/textual not installed")
class ComposeTests(BoardCase):
    async def send(self, pilot, line):
        await pilot.press("m")
        await type_text(pilot, line)
        await pilot.press("enter")
        await pilot.pause()

    async def test_m_types_into_input_and_enter_posts_as_operator(self):
        app = self.make_app()
        async with app.run_test(size=SIZE) as pilot:
            await self.load(app, pilot)
            self.assertFalse(app.query_one("#compose").display)
            self.assertEqual(app.focused.id, "posts")
            await pilot.press("m")
            await pilot.pause()
            self.assertTrue(app.query_one("#compose").display)
            self.assertEqual(app.focused.id, "compose-input")
            self.assertIn("@w1", text_of(app.query_one("#compose-to")))
            await type_text(pilot, "jqkbmdlzpf o")
            self.assertTrue(app.is_running)
            self.assertEqual(app.query_one("#compose-input").value, "jqkbmdlzpf o")
            self.assertEqual((app.state.sel, app.state.filter, app.state.zoom, app.state.follow), (3, None, False, True))
            self.assertEqual(len(self.stream()), len(POSTS))
            await pilot.press("enter")
            await pilot.pause()
            sent = self.stream()[-1]
            self.assertEqual(
                (sent["from"], sent["to"], sent["body"], sent["kind"]), ("operator", "@w1", "jqkbmdlzpf o", "msg")
            )
            self.assertEqual(len(self.stream()), len(POSTS) + 1)
            self.assertFalse(app.query_one("#compose").display)
            self.assertEqual(app.query_one("#compose-input").value, "")
            self.assertEqual(app.focused.id, "posts")
            self.assertTrue(app.state.show_message)
            self.assertIn("sent to @w1", status_of(app))
            self.assertEqual(self.r.exists(store.k_presence(ROOM, "operator")), 1)
            await settle(pilot, lambda: len(app.state.posts) == len(POSTS) + 1)

    async def test_default_recipient_follows_focus(self):
        app = self.make_app()
        async with app.run_test(size=SIZE) as pilot:
            await self.load(app, pilot)
            await pilot.press("k", "m")
            self.assertEqual(app.state.compose_to, "@verifier")
            await pilot.press("escape", "tab")
            self.assertEqual(app.state.focus, "message")
            await pilot.press("m")
            self.assertEqual(app.state.compose_to, "@verifier")
            await pilot.press("escape", "tab")
            self.assertEqual(app.state.focus, "roster")
            app.state.member = "coordinator"
            await pilot.press("m")
            self.assertEqual(app.state.compose_to, "@coordinator")
            await pilot.press("escape")
            self.assertEqual(app.focused.id, "roster")

    async def test_empty_room_defaults_to_all_and_tab_stays(self):
        self.r.flushall()
        app = self.make_app()
        async with app.run_test(size=SIZE) as pilot:
            await self.load(app, pilot)
            await pilot.press("m", "tab")
            self.assertEqual(app.state.compose_to, "all")
            await type_text(pilot, "hi")
            await pilot.press("enter")
            await pilot.pause()
            self.assertEqual(self.stream()[-1]["to"], "all")

    async def test_tab_toggles_between_default_and_all(self):
        app = self.make_app()
        async with app.run_test(size=SIZE) as pilot:
            await self.load(app, pilot)
            await pilot.press("m", "tab")
            self.assertEqual(app.state.compose_to, "all")
            self.assertIn("all", text_of(app.query_one("#compose-to")))
            await pilot.press("tab")
            self.assertEqual(app.state.compose_to, "@w1")
            await pilot.press("shift+tab")
            self.assertEqual(app.state.compose_to, "all")
            self.assertEqual((app.focused.id, app.state.focus), ("compose-input", "posts"))
            await type_text(pilot, "everyone")
            await pilot.press("enter")
            await pilot.pause()
            self.assertEqual((self.stream()[-1]["to"], self.stream()[-1]["body"]), ("all", "everyone"))

    async def test_typed_recipient_overrides(self):
        app = self.make_app()
        cases = [
            ("@verifier please  check", "@verifier", "please  check"),
            ("/to w1,verifier  two  spaces", "w1,verifier", "two  spaces"),
            ("@all stand down", "all", "stand down"),
            ("/to @w1,all hi", "all", "hi"),
            ("/tomorrow works", "@w1", "/tomorrow works"),
        ]
        async with app.run_test(size=SIZE) as pilot:
            await self.load(app, pilot)
            for line, to, body in cases:
                with self.subTest(line=line):
                    app.state.sel = 3
                    app.state.follow = False
                    await self.send(pilot, line)
                    self.assertEqual((self.stream()[-1]["to"], self.stream()[-1]["body"]), (to, body))

    async def test_bad_recipient_or_missing_body_keeps_input(self):
        app = self.make_app()
        async with app.run_test(size=SIZE) as pilot:
            await self.load(app, pilot)
            for line, complaint in (
                ("@bad! hi", "recipient"),
                ("/to w1, hi", "recipient"),
                ("/to w1", "message"),
                ("@verifier", "message"),
                ("/to", "message"),
            ):
                with self.subTest(line=line):
                    await self.send(pilot, line)
                    self.assertTrue(app.query_one("#compose").display)
                    self.assertEqual(app.query_one("#compose-input").value, line)
                    self.assertIn(complaint, status_of(app))
                    await pilot.press("escape")
            self.assertEqual(len(self.stream()), len(POSTS))

    async def test_unknown_mention_is_sent_with_a_warning(self):
        app = self.make_app()
        async with app.run_test(size=SIZE) as pilot:
            await self.load(app, pilot)
            await self.send(pilot, "@nobody hello")
            self.assertEqual(self.stream()[-1]["to"], "@nobody")
            self.assertIn("no member or group named nobody", status_of(app))

    async def test_escape_and_blank_enter_post_nothing(self):
        app = self.make_app()
        async with app.run_test(size=SIZE) as pilot:
            await self.load(app, pilot)
            await pilot.press("p")
            await pilot.press("m")
            await type_text(pilot, "draft")
            await pilot.press("escape")
            await pilot.pause()
            self.assertFalse(app.query_one("#compose").display)
            self.assertEqual(app.focused.id, "posts")
            self.assertEqual(app.state.filter, "coordinator")
            await self.send(pilot, "  ")
            self.assertFalse(app.query_one("#compose").display)
            await pilot.press("m")
            self.assertEqual(app.query_one("#compose-input").value, "")
            self.assertEqual(len(self.stream()), len(POSTS))

    async def test_input_survives_polls_clicks_and_navigation_keys(self):
        app = self.make_app()
        async with app.run_test(size=SIZE) as pilot:
            await self.load(app, pilot)
            await pilot.press("m")
            await type_text(pilot, "he")
            self.post(10, "verifier", "all", "arrived while typing")
            await self.load(app, pilot)
            await type_text(pilot, "l")
            await pilot.press("up", "down", "pageup", "pagedown")
            await type_text(pilot, "l")
            self.assertTrue(await pilot.click("#posts", offset=(5, 0)))
            await pilot.pause()
            await type_text(pilot, "o")
            self.assertTrue(await pilot.click("#header", offset=(1, 0)))
            await pilot.pause()
            await type_text(pilot, "q")
            self.assertTrue(app.is_running)
            self.assertEqual(app.focused.id, "compose-input")
            self.assertEqual(app.query_one("#compose-input").value, "helloq")
            self.assertEqual(app.state.compose_to, "@w1")

    async def test_compose_row_does_not_overlap_the_footer(self):
        app = self.make_app()
        async with app.run_test(size=SIZE) as pilot:
            await self.load(app, pilot)
            await pilot.press("m")
            await pilot.pause()
            compose, foot = app.query_one("#compose").region, app.query_one("#foot").region
            self.assertEqual(compose.height, 1)
            self.assertEqual(compose.bottom, foot.y)
            self.assertEqual(foot.bottom, SIZE[1])
            self.assertEqual(app.query_one("#compose-input").region.height, 1)
            self.assertGreater(app.query_one("#compose-input").region.width, 60)

    async def test_redis_error_keeps_the_draft(self):
        app = self.make_app()
        async with app.run_test(size=SIZE) as pilot:
            await self.load(app, pilot)
            with mock.patch.object(self.r, "xadd", side_effect=redis.ConnectionError("down [bold]")):
                await self.send(pilot, "keep me")
            self.assertIn("redis: down [bold]", status_of(app))
            self.assertEqual(app.query_one("#compose-input").value, "keep me")
            await self.load(app, pilot)
            self.assertIn("redis: down", status_of(app))
            await pilot.press("enter")
            await pilot.pause()
            self.assertEqual(self.stream()[-1]["body"], "keep me")
            self.assertFalse(app.query_one("#compose").display)

    async def test_status_confirmation_gives_way_to_the_next_key(self):
        app = self.make_app()
        async with app.run_test(size=SIZE) as pilot:
            await self.load(app, pilot)
            await self.send(pilot, "hi")
            self.assertIn("sent to", status_of(app))
            await pilot.press("k")
            self.assertTrue(status_of(app).startswith("Post "))


@unittest.skipIf(fakeredis is None, "fakeredis/textual not installed")
class FinishTests(BoardCase):
    PROMPT = f'Finish swarm "{ROOM}" (--kind done to all)? press y to confirm, esc cancels'

    async def test_d_then_y_posts_done_from_coordinator(self):
        app = self.make_app()
        async with app.run_test(size=SIZE) as pilot:
            await self.load(app, pilot)
            await pilot.press("d")
            self.assertTrue(app.state.pending_finish)
            self.assertEqual(status_of(app), self.PROMPT)
            await self.load(app, pilot)
            self.assertEqual(status_of(app), self.PROMPT)
            self.assertEqual(len(self.stream()), len(POSTS))
            await pilot.press("y")
            self.assertFalse(app.state.pending_finish)
            done = self.stream()[-1]
            self.assertEqual(
                (done["from"], done["to"], done["kind"], done["body"]),
                ("coordinator", "all", "done", "Finished from board."),
            )
            await settle(pilot, lambda: "✓ finished" in text_of(app.query_one("#header")))
            await pilot.press("y")
            self.assertEqual(len(self.stream()), len(POSTS) + 1)

    async def test_sender_is_the_member_holding_the_coordinator_role(self):
        self.r.hset(store.k_member(ROOM, "coordinator"), "role", "worker")
        self.member("zed", role="coordinator", state="waiting")
        app = self.make_app()
        async with app.run_test(size=SIZE) as pilot:
            await self.load(app, pilot)
            await pilot.press("d", "y")
            self.assertEqual(self.stream()[-1]["from"], "zed")

    async def test_sender_falls_back_to_coordinator(self):
        self.r.flushall()
        self.member("w1", role="worker", state="waiting")
        app = self.make_app()
        async with app.run_test(size=SIZE) as pilot:
            await self.load(app, pilot)
            await pilot.press("d", "y")
            self.assertEqual(self.stream()[-1]["from"], "coordinator")

    async def test_any_other_key_cancels_without_acting(self):
        app = self.make_app()
        async with app.run_test(size=SIZE) as pilot:
            await self.load(app, pilot)
            for key in ("q", "j", "k", "tab", "escape", "b", "m", "l", "d", "p", "z", "f", "enter", "space", "n"):
                with self.subTest(key=key):
                    await pilot.press("d")
                    self.assertTrue(app.state.pending_finish)
                    await pilot.press(key)
                    await pilot.pause()
                    st = app.state
                    self.assertTrue(app.is_running)
                    self.assertFalse(st.pending_finish)
                    self.assertEqual(status_of(app), "cancelled")
                    self.assertEqual(
                        (st.sel, st.focus, st.filter, st.zoom, st.follow, st.show_message, st.show_log, st.expanded),
                        (3, "posts", None, False, True, True, False, set()),
                    )
                    self.assertFalse(app.query_one("#compose").display)
            await pilot.press("y")
            self.assertEqual(len(self.stream()), len(POSTS))

    async def test_click_cancels_the_prompt(self):
        app = self.make_app()
        async with app.run_test(size=SIZE) as pilot:
            await self.load(app, pilot)
            await pilot.press("d")
            await pilot.click("#header", offset=(1, 0))
            await pilot.pause()
            self.assertFalse(app.state.pending_finish)
            await pilot.press("y")
            self.assertEqual(len(self.stream()), len(POSTS))

    async def test_redis_error_is_shown(self):
        app = self.make_app()
        async with app.run_test(size=SIZE) as pilot:
            await self.load(app, pilot)
            with mock.patch.object(self.r, "xadd", side_effect=redis.ConnectionError("down")):
                await pilot.press("d", "y")
            self.assertFalse(app.state.pending_finish)
            self.assertIn("redis: down", status_of(app))


@unittest.skipIf(fakeredis is None, "fakeredis/textual not installed")
class LogPeekTests(BoardCase):
    def setUp(self):
        super().setUp()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = tmp.name
        patch = mock.patch.dict(os.environ, {"SWARM_STATE_DIR": self.root})
        patch.start()
        self.addCleanup(patch.stop)
        os.makedirs(os.path.join(self.root, ROOM))
        self.log = os.path.join(self.root, ROOM, "w1.log")

    def write_log(self, text, mode="w"):
        with open(self.log, mode, encoding="utf-8") as handle:
            handle.write(text)

    def numbered(self, count):
        self.write_log("".join(f"line-{i:03}\n" for i in range(1, count + 1)))

    def body(self, app):
        return text_of(app.query_one("#message")).split("\n")

    def label(self, app):
        return text_of(app.query_one("#message-label"))

    async def open_log(self, app, pilot, member="w1"):
        await self.load(app, pilot)
        app.state.member = member
        await pilot.press("l")
        await pilot.pause()

    async def test_l_shows_the_tail_and_scrolls(self):
        self.numbered(250)
        app = self.make_app()
        async with app.run_test(size=SIZE) as pilot:
            await self.open_log(app, pilot)
            height = app.query_one("#message").size.height
            self.assertEqual(app.focused.id, "message")
            self.assertTrue(self.label(app).startswith(f"Log · w1 · {self.log} · lines "))
            self.assertTrue(self.label(app).endswith(f"lines {201 - height}-200 of 200"))
            self.assertEqual(self.body(app)[-1], "line-250")
            await pilot.press("k")
            self.assertEqual(self.body(app)[-1], "line-249")
            await pilot.press("up")
            self.assertEqual(self.body(app)[-1], "line-248")
            await pilot.press("j", "down")
            self.assertEqual(self.body(app)[-1], "line-250")
            await pilot.press("pageup")
            self.assertEqual(self.body(app)[-1], f"line-{250 - height:03}")
            await pilot.press(*["pageup"] * 40)
            self.assertEqual(self.body(app)[0], "line-051")
            await pilot.press("pagedown")
            self.assertEqual(self.body(app)[0], f"line-{51 + height:03}")

    async def test_tail_follows_new_lines_only_at_the_bottom(self):
        self.numbered(300)
        app = self.make_app()
        async with app.run_test(size=SIZE) as pilot:
            await self.open_log(app, pilot)
            self.write_log("fresh-1\n", "a")
            await self.load(app, pilot)
            self.assertEqual(self.body(app)[-1], "fresh-1")
            await pilot.press("k", "k")
            held = self.body(app)
            self.write_log("fresh-2\n", "a")
            await self.load(app, pilot)
            self.assertEqual(self.body(app), held)
            await pilot.press("j", "j", "j")
            self.assertEqual(self.body(app)[-1], "fresh-2")
            self.write_log("fresh-3\n", "a")
            await self.load(app, pilot)
            self.assertEqual(self.body(app)[-1], "fresh-3")

    async def test_log_text_is_literal_and_long_lines_wrap(self):
        self.write_log("[bold red]markup[/]\n\x1b[31mred\x1b[0m\ttab\r\n" + "x" * 300 + "END\n")
        with open(self.log, "ab") as handle:
            handle.write(b"bad\xffbyte\n")
        app = self.make_app()
        async with app.run_test(size=SIZE) as pilot:
            await self.open_log(app, pilot)
            lines = self.body(app)
            width = app.query_one("#message").size.width
            self.assertEqual(lines[0], "[bold red]markup[/]")
            self.assertEqual(lines[1], "\\x1b[31mred\\x1b[0m    tab")
            self.assertTrue(all(len(line) <= width for line in lines))
            self.assertEqual("".join(lines[2:-1]), "x" * 300 + "END")
            self.assertEqual(lines[-1], "bad\ufffdbyte")

    async def test_huge_log_reads_only_the_tail(self):
        self.write_log(("y" * 100 + "\n") * 5000 + "last\n")
        app = self.make_app()
        async with app.run_test(size=SIZE) as pilot:
            await self.open_log(app, pilot)
            self.assertEqual(self.body(app)[-1], "last")
            self.assertTrue(self.label(app).endswith("of 200"))

    async def test_missing_log_and_l_or_escape_close(self):
        app = self.make_app()
        async with app.run_test(size=SIZE) as pilot:
            await self.open_log(app, pilot)
            shown = "".join(self.body(app))
            self.assertEqual(shown, f"(no log file at {self.log})")
            await pilot.press("l")
            await pilot.pause()
            self.assertFalse(app.state.show_log)
            self.assertTrue(self.label(app).startswith("Message · #4"))
            self.assertEqual(app.focused.id, "posts")
            await pilot.press("p", "l")
            await pilot.press("escape")
            await pilot.pause()
            self.assertFalse(app.state.show_log)
            self.assertEqual(app.state.filter, "coordinator")
            self.assertTrue(self.label(app).startswith("Message · "))
            self.assertEqual(app.focused.id, "posts")

    async def test_a_taller_pane_re_pins_a_scrolled_log_to_the_tail(self):
        self.numbered(60)
        app = self.make_app()
        async with app.run_test(size=(120, 40)) as pilot:
            await self.open_log(app, pilot)
            await pilot.press("k", "k")
            await pilot.resize_terminal(120, 220)
            await pilot.pause()
            self.write_log("after-resize\n", "a")
            await self.load(app, pilot)
            self.assertEqual(self.body(app)[-1], "after-resize")

    async def test_unreadable_log_says_so(self):
        self.numbered(3)
        app = self.make_app()
        async with app.run_test(size=SIZE) as pilot:
            with mock.patch.object(board, "read_log_tail", side_effect=PermissionError(13, "Permission denied")):
                await self.open_log(app, pilot)
                self.assertIn("(cannot read log file at", "".join(self.body(app)))

    async def test_log_opens_over_a_hidden_message_pane_and_enter_closes_it(self):
        self.numbered(5)
        app = self.make_app()
        async with app.run_test(size=SIZE) as pilot:
            await self.load(app, pilot)
            await pilot.press("enter")
            self.assertFalse(app.query_one("#msg-sec").display)
            app.state.member = "w1"
            await pilot.press("l")
            await pilot.pause()
            self.assertTrue(app.query_one("#msg-sec").display)
            self.assertEqual(self.body(app)[-1], "line-005")
            await pilot.press("tab", "tab", "tab")
            self.assertEqual(app.state.focus, "message")
            await pilot.press("enter")
            await pilot.pause()
            self.assertFalse(app.state.show_log)
            self.assertFalse(app.query_one("#msg-sec").display)
            self.assertEqual(app.focused.id, "posts")

    async def test_log_follows_the_selected_member_and_survives_zoom(self):
        self.numbered(5)
        app = self.make_app()
        async with app.run_test(size=SIZE) as pilot:
            await self.open_log(app, pilot)
            await pilot.press("z")
            await pilot.pause()
            self.assertEqual(self.body(app)[-1], "line-005")
            await pilot.press("escape")
            self.assertFalse(app.state.show_log)
            self.assertEqual(app.focused.id, "message" if app.state.zoom else "posts")
            await pilot.press("escape", "l")
            app.state.member = "verifier"
            app.render_all()
            self.assertTrue(self.label(app).startswith("Log · verifier · "))

    async def test_member_name_cannot_escape_the_room_directory(self):
        with open(os.path.join(self.root, "secret.log"), "w", encoding="utf-8") as handle:
            handle.write("TOPSECRET\n")
        self.post(8, "../secret", "all", "hi")
        app = self.make_app()
        async with app.run_test(size=SIZE) as pilot:
            await self.open_log(app, pilot, member="../secret")
            self.assertNotIn("TOPSECRET", "".join(self.body(app)))
            self.assertIn("(no log file at", "".join(self.body(app)))


@unittest.skipIf(fakeredis is None, "fakeredis/textual not installed")
class MouseTests(BoardCase):
    def add_workers(self, *names):
        for name in names:
            self.member(name, role="worker", model="claude-sonnet-5-5", state="working")

    async def click(self, pilot, selector, x, y):
        self.assertTrue(await pilot.click(selector, offset=(x + 1, y)))
        await pilot.pause()

    async def wheel(self, pilot, selector, direction):
        kind = events.MouseScrollUp if direction < 0 else events.MouseScrollDown
        self.assertTrue(await pilot._post_mouse_events([kind], selector, offset=(3, 0)))
        await pilot.pause()

    async def test_clicking_a_roster_card_selects_it(self):
        self.add_workers("w2", "w3")
        app = self.make_app()
        async with app.run_test(size=SIZE) as pilot:
            await self.load(app, pilot)
            colw = board.card_width(app.query_one("#roster").size.width)
            stride, row = colw + 2, board.CARD_LINES + 1
            await self.click(pilot, "#roster", stride, 0)
            self.assertEqual((app.state.member_name(), app.state.focus), ("verifier", "roster"))
            self.assertEqual(app.focused.id, "roster")
            await self.click(pilot, "#roster", 2 * stride + colw - 1, board.CARD_LINES - 1)
            self.assertEqual(app.state.member_name(), "w1")
            await self.click(pilot, "#roster", colw - 1, row)
            self.assertEqual(app.state.member_name(), "w2")
            await self.click(pilot, "#roster", stride, row + 2)
            self.assertEqual(app.state.member_name(), "w3")
            for x, y in ((2 * stride, row), (colw, 0), (0, board.CARD_LINES)):
                await self.click(pilot, "#roster", x, y)
                self.assertEqual(app.state.member_name(), "w3", (x, y))

    async def test_clicking_a_scrolled_roster_and_the_retired_line(self):
        self.add_workers("w2", "w3", "w4", "w5", "w6")
        self.member("zz", role="worker", state="retired")
        app = self.make_app()
        async with app.run_test(size=SIZE) as pilot:
            await self.load(app, pilot)
            await pilot.press("tab", "tab", "down", "down")
            self.assertEqual(app.state.roster_top, 1)
            await self.click(pilot, "#roster", 0, 0)
            self.assertEqual(app.state.member_name(), app.state.active()[3])
            retired_row = app.query_one("#roster").size.height - 1
            await self.click(pilot, "#roster", 0, retired_row)
            self.assertEqual(app.state.member_name(), app.state.active()[3])

    async def test_clicking_a_post_selects_it_and_stops_following(self):
        app = self.make_app()
        async with app.run_test(size=SIZE) as pilot:
            await self.load(app, pilot)
            await pilot.press("tab")
            await self.click(pilot, "#posts", 10, 1)
            st = app.state
            self.assertEqual((st.sel, st.member, st.follow, st.focus), (1, "w1", False, "posts"))
            self.assertEqual(app.focused.id, "posts")
            self.post(9, "verifier", "all", "newer")
            await self.load(app, pilot)
            self.assertEqual(st.sel, 1)
            blank_row = app.query_one("#posts").size.height - 1
            self.assertGreater(blank_row, len(st.posts))
            await self.click(pilot, "#posts", 10, blank_row)
            self.assertEqual(st.sel, 1)

    async def test_clicking_around_an_expanded_post(self):
        app = self.make_app()
        async with app.run_test(size=SIZE) as pilot:
            await self.load(app, pilot)
            await pilot.press("k", "k", "k", "space")
            await pilot.pause()
            rows = text_of(app.query_one("#posts")).split("\n")
            second = next(i for i, row in enumerate(rows) if "#2 " in row)
            self.assertGreater(second, 1)
            await self.click(pilot, "#posts", 10, second)
            self.assertEqual((app.state.sel, app.state.member), (1, "w1"))
            await self.click(pilot, "#posts", 10, second - 1)
            self.assertEqual((app.state.sel, app.state.member), (0, "coordinator"))

    async def test_a_long_code_fence_line_does_not_shift_clicks(self):
        self.r.flushall()
        self.post(0, "w1", "all", "```" + "y" * 300 + "\ncode\n```")
        self.post(1, "verifier", "all", "after")
        app = self.make_app()
        async with app.run_test(size=SIZE) as pilot:
            await self.load(app, pilot)
            await pilot.press("k", "space")
            await pilot.pause()
            width = app.query_one("#posts").size.width
            rows = text_of(app.query_one("#posts")).split("\n")
            self.assertTrue(all(len(row) <= width for row in rows), rows)

    async def test_clicking_a_scrolled_posts_pane(self):
        for i in range(10, 50):
            self.post(i, "w1" if i % 2 else "verifier", "all", f"post {i}")
        app = self.make_app()
        async with app.run_test(size=SIZE) as pilot:
            await self.load(app, pilot)
            self.assertGreater(app.state.post_top, 0)
            top = app.state.post_top
            await self.click(pilot, "#posts", 10, 0)
            self.assertEqual((app.state.sel, app.state.post_top), (top, top))
            self.assertTrue(text_of(app.query_one("#posts")).startswith("> "))
            await self.click(pilot, "#posts", 10, 2)
            self.assertEqual(app.state.sel, top + 2)

    async def test_wheel_moves_posts_roster_and_message(self):
        self.add_workers("w2", "w3")
        self.post(9, "w1", "all", "\n".join(f"row {i}" for i in range(80)))
        app = self.make_app()
        async with app.run_test(size=SIZE) as pilot:
            await self.load(app, pilot)
            st = app.state
            await self.wheel(pilot, "#message", 1)
            self.assertEqual(st.msg_top, 3)
            self.assertTrue(text_of(app.query_one("#message")).startswith("row 3\n"))
            await self.wheel(pilot, "#message", -1)
            await self.wheel(pilot, "#message", -1)
            self.assertEqual(st.msg_top, 0)
            await self.wheel(pilot, "#posts", -1)
            self.assertEqual((st.sel, st.follow), (3, False))
            await self.wheel(pilot, "#posts", 1)
            self.assertEqual(st.sel, 4)
            st.member = "coordinator"
            await self.wheel(pilot, "#roster", 1)
            self.assertEqual(st.member_name(), "w2")
            await self.wheel(pilot, "#roster", -1)
            self.assertEqual(st.member_name(), "coordinator")
            self.assertEqual(st.focus, "posts")

    async def test_wheel_scrolls_the_log(self):
        with tempfile.TemporaryDirectory() as root, mock.patch.dict(os.environ, {"SWARM_STATE_DIR": root}):
            os.makedirs(os.path.join(root, ROOM))
            with open(os.path.join(root, ROOM, "coordinator.log"), "w", encoding="utf-8") as handle:
                handle.write("".join(f"l{i}\n" for i in range(100)))
            app = self.make_app()
            async with app.run_test(size=SIZE) as pilot:
                await self.load(app, pilot)
                await pilot.press("l")
                await pilot.pause()
                await self.wheel(pilot, "#message", -1)
                self.assertTrue(text_of(app.query_one("#message")).endswith("l96"))
                await self.wheel(pilot, "#message", 1)
                self.assertTrue(text_of(app.query_one("#message")).endswith("l99"))

    async def test_hints_name_the_new_keys(self):
        for hint in ("m msg", "l log", "d done"):
            self.assertIn(hint, board.HINTS)
        self.assertLessEqual(len(board.HINTS), 102)


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
