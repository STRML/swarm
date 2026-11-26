"""Pilot tests for `swarm board` room picker and its hand-off with SwarmBoard."""
import time
import unittest
import uuid
from unittest import mock
from urllib.parse import urlparse

try:
    import fakeredis
    import redis
    import textual  # noqa: F401
except ImportError:  # pragma: no cover
    fakeredis = None

from swarm import board, store
try:
    from tests.test_board import ROOM, SIZE
    from tests.test_worker_e2e import URL, redis_up
except ModuleNotFoundError:
    from test_board import ROOM, SIZE
    from test_worker_e2e import URL, redis_up

HOUR_MS = 3_600_000


def seed(r, room, age_ms=0, count=2, title=None, present=0):
    base = int(time.time() * 1000) - age_ms
    for i in range(count):
        r.xadd(store.k_stream(room), {"from": "a", "to": "all", "body": f"m{i}", "kind": "msg"}, id=f"{base + i}-0")
    if title:
        r.hset(store.k_meta(room), "title", title)
    for i in range(present):
        r.set(store.k_presence(room, f"w{i}"), "now", ex=90)


def cell_text(app):
    table = app.query_one(board.DataTable)
    return [[cell.plain for cell in table.get_row_at(i)] for i in range(table.row_count)]


async def settle(pilot, predicate, timeout=5):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return
        await pilot.pause(0.05)
    raise AssertionError("timed out waiting")


@unittest.skipIf(fakeredis is None, "fakeredis/textual not installed")
class PickerDisplayTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.r = fakeredis.FakeStrictRedis(decode_responses=True)
        seed(self.r, "swarm-old", age_ms=30 * HOUR_MS, count=5, title="Epic 61")
        seed(self.r, "swarm-new", age_ms=60_000, count=3, title="Epic 62", present=2)

    def make_app(self):
        return board.RoomPicker(self.r, idle_hours=0, interval=0)

    async def load(self, app, pilot):
        app.apply(store.list_rooms(self.r))
        await pilot.pause()

    async def test_lists_rooms_newest_first(self):
        app = self.make_app()
        async with app.run_test(size=SIZE) as pilot:
            await self.load(app, pilot)
            self.assertEqual(
                cell_text(app),
                [["swarm-new", "Epic 62", "3", "1m ago", "2"], ["swarm-old", "Epic 61", "5", "1d ago", "0"]],
            )

    async def test_enter_opens_selected_room(self):
        app = self.make_app()
        async with app.run_test(size=SIZE) as pilot:
            await self.load(app, pilot)
            await pilot.press("down", "enter")
        self.assertEqual(app.return_value, "swarm-old")

    async def test_x_confirm_prompt_cancels_on_other_keys(self):
        app = self.make_app()
        with mock.patch.object(store, "clear_room") as clear:
            async with app.run_test(size=SIZE) as pilot:
                await self.load(app, pilot)
                await pilot.press("x", "enter")
                await pilot.pause()
                self.assertTrue(app.is_running)
                self.assertIsNone(app.pending)
                self.assertEqual(app.query_one("#status").content.plain, "cancelled")
        clear.assert_not_called()

    async def test_board_b_goes_back_to_picker(self):
        app = board.SwarmBoard(self.r, ROOM, interval=0)
        async with app.run_test(size=SIZE) as pilot:
            await pilot.press("b")
        self.assertEqual(app.return_value, board.PICKER)

    def test_browse_alternates_picker_and_board(self):
        picker, boards = mock.Mock(), mock.Mock()
        picker.return_value.run.side_effect = ["a", "b", None]
        boards.return_value.run.side_effect = [board.PICKER, board.PICKER]
        with mock.patch.object(board, "RoomPicker", picker), mock.patch.object(board, "SwarmBoard", boards):
            board.browse(self.r, None)
        self.assertEqual([c.args[1] for c in boards.call_args_list], ["a", "b"])

    async def test_force_hint_uses_swarm_cli(self):
        app = board.RoomPicker(self.r, idle_hours=0, interval=0, url="redis://h:6379/3")
        async with app.run_test(size=SIZE) as pilot:
            await self.load(app, pilot)
            app.finish("swarm-new", {"status": "present", "present": 2}, "clear")
            self.assertIn("swarm --url redis://h:6379/3 clear --room swarm-new --force",
                          app.query_one("#status").content.plain)


@unittest.skipUnless(fakeredis is not None and redis_up(), "needs textual and a local Redis")
class PickerWritesE2E(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.r = redis.Redis.from_url(URL, decode_responses=True)
        self.sweep_r = redis.Redis.from_url(urlparse(URL)._replace(path="/14").geturl(), decode_responses=True)
        self.tag = "swt-" + uuid.uuid4().hex[:8]
        self.rooms = {name: f"{self.tag}-{name}" for name in ("idle", "busy", "fresh")}

    def tearDown(self):
        prefix = store.key_prefix()
        for client in (self.r, self.sweep_r):
            for key in client.scan_iter(match=f"{prefix}:*:{self.tag}*"):
                client.delete(key)

    def make_app(self, idle_hours=0, r=None):
        return board.RoomPicker(r or self.r, idle_hours=idle_hours, interval=0)

    async def select(self, app, pilot, room):
        app.apply(store.list_rooms(self.r))
        await pilot.pause()
        app.query_one(board.DataTable).move_cursor(row=[row["room"] for row in app.rooms].index(room))
        await pilot.pause()

    async def test_a_archives_selected_room(self):
        room = self.rooms["fresh"]
        seed(self.r, room)
        app = self.make_app()
        async with app.run_test(size=SIZE) as pilot:
            await self.select(app, pilot, room)
            await pilot.press("a")
            await settle(pilot, lambda: room not in [row["room"] for row in app.rooms])
            self.assertIn("archived", app.query_one("#status").content.plain)
        self.assertEqual(self.r.exists(store.k_stream(room)), 0)

    async def test_x_then_y_clears_selected_room(self):
        room = self.rooms["fresh"]
        seed(self.r, room, title="t")
        self.r.set(store.k_cursor(room, "w1"), "0-0")
        app = self.make_app()
        async with app.run_test(size=SIZE) as pilot:
            await self.select(app, pilot, room)
            await pilot.press("x", "y")
            await settle(pilot, lambda: room not in [row["room"] for row in app.rooms])
        prefix = store.key_prefix()
        self.assertEqual(list(self.r.scan_iter(match=f"{prefix}:*:{room}*")), [])

    async def test_opening_picker_sweeps_idle_rooms_without_touching_present(self):
        if self.sweep_r.dbsize():
            self.skipTest("db 14 is not empty")
        seed(self.sweep_r, self.rooms["idle"], age_ms=8 * HOUR_MS)
        seed(self.sweep_r, self.rooms["busy"], age_ms=8 * HOUR_MS, present=1)
        seed(self.sweep_r, self.rooms["fresh"])
        app = self.make_app(idle_hours=6, r=self.sweep_r)
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot, lambda: self.rooms["idle"] in app.notes)
            names = [row["room"] for row in app.rooms]
            self.assertNotIn(self.rooms["idle"], names)
            self.assertIn(self.rooms["busy"], names)
            self.assertIn(self.rooms["fresh"], names)


if __name__ == "__main__":
    unittest.main()
