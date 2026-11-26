"""End-to-end: `swarm rooms / archive / unarchive / clear` against real local Redis (db 15 by default)."""
import json
import subprocess
import sys
import threading
import time
import unittest

from swarm import store
try:
    from tests.test_worker_e2e import URL, SwarmE2E, redis_up
except ModuleNotFoundError:
    from test_worker_e2e import URL, SwarmE2E, redis_up

DAY_MS = 86_400_000


@unittest.skipUnless(redis_up(), "local Redis not reachable")
class RoomsE2E(SwarmE2E):
    def setUp(self):
        super().setUp()
        self.env["SWARM_ARCHIVE_IDLE_HOURS"] = "0"
        self.extra = []

    def tearDown(self):
        prefix = store.key_prefix()
        for room in self.extra:
            for key in self.r.scan_iter(match=f"{prefix}:*:{room}*"):
                self.r.delete(key)
        super().tearDown()

    def cli(self, *args, **env):
        return subprocess.run(
            [sys.executable, "-m", "swarm", "--url", URL, *args],
            capture_output=True,
            text=True,
            env=dict(self.env, **env),
        )

    def other_room(self, suffix="b"):
        room = f"{self.room}-{suffix}"
        self.extra.append(room)
        return room

    def fill(self, room, count=3, age_ms=0, title=None):
        base = int(time.time() * 1000) - age_ms
        for i in range(count):
            self.r.xadd(
                store.k_stream(room),
                {"from": "a", "to": "all", "body": f"m{i}", "kind": "msg"},
                id=f"{base + i}-0",
            )
        if title:
            self.r.hset(store.k_meta(room), "title", title)

    def join(self, agent, room=None, present=True):
        room = room or self.room
        self.swarm("--room", room, "join", "--as", agent, "--role", "worker", "--group", "api")
        if not present:
            self.r.delete(store.k_presence(room, agent))

    def rooms(self, *args, **env):
        res = self.cli("rooms", "--json", *args, **env)
        self.assertEqual(res.returncode, 0, res.stderr)
        return {row.get("room"): row for row in json.loads(res.stdout)}

    def archive_keys(self, room=None):
        return sorted(self.r.scan_iter(match=f"{store.archive_prefix()}{room or self.room}:*"))

    def live_keys(self, room=None):
        p = store.key_prefix()
        rname = room or self.room
        return sorted(self.r.scan_iter(match=f"{p}:*:{rname}:*")) + (
            [store.k_stream(rname)] if self.r.exists(store.k_stream(rname)) else []
        )

    def test_rooms_lists_title_messages_activity_and_present(self):
        self.fill(self.room, count=3, age_ms=2 * 3_600_000, title="Epic 62")
        self.join("w1")
        row = self.rooms()[self.room]
        self.assertEqual((row["title"], row["messages"], row["present"]), ("Epic 62", 3, 1))
        self.assertRegex(row["last_activity"], r"^\d{4}-\d\d-\d\dT")

    def test_rooms_newest_activity_first_and_plain_output(self):
        newer, older = self.other_room("new"), self.other_room("old")
        self.fill(older, age_ms=5 * 3_600_000)
        self.fill(newer, age_ms=60_000)
        names = list(self.rooms())
        self.assertLess(names.index(newer), names.index(older))
        out = self.cli("rooms").stdout.splitlines()
        self.assertEqual(out[0], "ROOM\tTITLE\tMESSAGES\tLAST_ACTIVITY\tPRESENT")

    def test_archive_unarchive_round_trip_restores_everything_without_ttl(self):
        self.fill(self.room, count=3, title="Epic 62")
        self.join("w1", present=False)
        before = self.r.xrange(store.k_stream(self.room))
        res = self.cli("--room", self.room, "archive", "--json")
        self.assertEqual(res.returncode, 0, res.stderr)
        aid = json.loads(res.stdout)["archive_id"]
        self.assertFalse(
            self.r.exists(
                store.k_stream(self.room),
                store.k_meta(self.room),
                store.k_member(self.room, "w1"),
                store.k_groups(self.room, "w1"),
            )
        )
        self.assertEqual(self.r.exists(store.k_cursor(self.room, "w1")), 1)
        self.assertEqual(len(self.archive_keys()), 4)
        self.assertNotIn(self.room, self.rooms())
        listed = json.loads(self.cli("rooms", "--archived", "--json").stdout)
        entry = next(a for a in listed if a["archive_id"] == aid)
        self.assertEqual((entry["room"], entry["title"], entry["messages"]), (self.room, "Epic 62", 3))
        res = self.cli("unarchive", aid)
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(self.r.xrange(store.k_stream(self.room)), before)
        self.assertEqual(self.r.smembers(store.k_groups(self.room, "w1")), {"api"})
        self.assertEqual(self.r.hget(store.k_meta(self.room), "title"), "Epic 62")
        self.assertEqual(self.archive_keys(), [])
        self.assertEqual(self.r.ttl(store.k_stream(self.room)), -1)

    def test_bad_retention_or_idle_env_fails_before_anything_moves(self):
        self.fill(self.room, age_ms=48 * 3_600_000)
        for name, value in (
            ("SWARM_ARCHIVE_RETAIN_DAYS", "soon"),
            ("SWARM_ARCHIVE_RETAIN_DAYS", "0"),
            ("SWARM_ARCHIVE_IDLE_HOURS", "nan"),
        ):
            cmd = ("rooms",) if name == "SWARM_ARCHIVE_IDLE_HOURS" else ("--room", self.room, "archive")
            res = self.cli(*cmd, **{name: value})
            self.assertEqual(res.returncode, 2)
            self.assertIn(name, res.stderr)
        self.assertEqual(self.r.exists(store.k_stream(self.room)), 1)

    def test_archive_refuses_while_a_member_is_present_and_force_overrides(self):
        self.fill(self.room)
        self.join("w1")
        res = self.cli("--room", self.room, "archive")
        self.assertEqual(res.returncode, 1)
        self.assertIn("REFUSING", res.stderr)
        self.assertEqual(self.cli("--room", self.room, "archive", "--force").returncode, 0)
        self.assertEqual(self.r.exists(store.k_stream(self.room)), 0)

    def test_idle_sweep_archives_idle_rooms_and_skips_present_ones(self):
        idle, busy, fresh = self.other_room("idle"), self.other_room("busy"), self.other_room("fresh")
        for room, age in ((idle, 8), (busy, 8), (fresh, 0)):
            self.fill(room, age_ms=age * 3_600_000)
        self.join("w1", room=busy)
        res = self.cli("archive", "--idle-hours", "6")
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(self.r.exists(store.k_stream(idle)), 0)
        self.assertEqual(self.r.exists(store.k_stream(busy), store.k_stream(fresh)), 2)

    def test_unarchive_refuses_when_live_name_is_taken(self):
        self.fill(self.room, count=2)
        aid = json.loads(self.cli("--room", self.room, "archive", "--json").stdout)["archive_id"]
        self.swarm("send", "--from", "coord", "after the archive")
        res = self.cli("unarchive", aid)
        self.assertEqual(res.returncode, 1)
        self.assertIn("REFUSING", res.stderr)

    def test_archive_and_clear_leave_prefix_sibling_rooms_alone(self):
        sibling = self.other_room("b")
        for room in (self.room, sibling):
            self.fill(room)
            self.join("w1", room=room, present=False)
        self.cli("--room", self.room, "archive")
        self.assertEqual(len(self.live_keys(sibling)), 4)
        self.cli("--room", self.room, "clear")
        self.assertEqual(len(self.live_keys(sibling)), 4)

    def test_clear_deletes_every_key_with_force(self):
        self.fill(self.room, title="Epic 62")
        self.join("w1")
        self.assertEqual(self.cli("--room", self.room, "clear").returncode, 1)
        self.assertEqual(self.cli("--room", self.room, "clear", "--force").returncode, 0)
        self.assertEqual(self.live_keys(), [])
        self.assertEqual(self.r.exists(store.k_meta(self.room)), 0)

    def test_archive_and_clear_require_explicit_room(self):
        self.fill(self.room)
        for cmd in ("archive", "clear"):
            res = self.cli(cmd, "--force")
            self.assertEqual(res.returncode, 2, cmd)
            self.assertIn("--room", res.stderr)
        self.assertEqual(self.r.exists(store.k_stream(self.room)), 1)

    def test_concurrent_sends_land_wholly_before_or_after_archive_and_clear(self):
        for op in ("archive", "clear"):
            room = self.other_room(op)
            for i in range(150):
                self.r.hset(store.k_member(room, f"w{i}"), mapping={"role": "worker"})
            self.fill(room, count=1)
            stop, sent = threading.Event(), []

            def hammer():
                writer = store.connect(URL)
                while not stop.is_set():
                    sent.append(writer.xadd(store.k_stream(room), {"from": "a", "to": "all", "body": "x", "kind": "msg"}))

            worker_t = threading.Thread(target=hammer, daemon=True)
            self.addCleanup(stop.set)
            worker_t.start()
            time.sleep(0.05)
            if op == "archive":
                result = store.archive_room(self.r, room, 60, force=True)
                self.assertEqual((result["status"], result["keys"]), ("ok", 151))
            else:
                result = store.clear_room(self.r, room, force=True)
                self.assertEqual(result["status"], "ok")
            stop.set()
            worker_t.join()
            live = self.r.xlen(store.k_stream(room))
            self.assertEqual(list(self.r.scan_iter(match=store.k_member(room, "*"))), [])
            if op == "archive":
                archived = self.r.xlen(f"{store.archive_prefix()}{result['archive_id']}:{store.k_stream(room)}")
                self.assertEqual(archived + live, len(sent) + 1)
            else:
                self.assertLessEqual(live, len(sent))


if __name__ == "__main__":
    unittest.main()
