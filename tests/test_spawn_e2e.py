"""End-to-end: `swarm spawn` picks a seat, joins the member, posts the brief and starts its worker."""
import json
import os
import shutil
import signal
import subprocess
import sys
import unittest

from swarm import store
try:
    from tests.test_worker_e2e import SRC, URL, SwarmE2E, redis_up
except ModuleNotFoundError:
    from test_worker_e2e import SRC, URL, SwarmE2E, redis_up


def stream_order(entry_id):
    return tuple(int(part) for part in (entry_id or "0-0").split("-"))


@unittest.skipUnless(redis_up(), "local Redis not reachable")
class SwarmSpawnE2E(SwarmE2E):
    def tearDown(self):
        for pid in getattr(self, "pids", []):
            try:
                os.kill(pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        super().tearDown()

    def spawn(self, *args, config=None):
        env = dict(self.env)
        if config:
            path = os.path.join(self.tmp, "swarm.yaml")
            with open(path, "w") as f:
                f.write(config)
            env["SWARM_CONFIG"] = path
        res = subprocess.run(
            [sys.executable, "-m", "swarm", "--url", URL, "--room", self.room, "spawn", *args],
            capture_output=True,
            text=True,
            env=env,
            timeout=30,
        )
        self.assertEqual(res.returncode, 0, res.stderr)
        out = json.loads(res.stdout)
        self.pids = getattr(self, "pids", []) + [out["pid"]]
        return out

    def test_spawn_with_explicit_harness_delivers_brief(self):
        brief = os.path.join(self.tmp, "brief.md")
        with open(brief, "w") as f:
            f.write("build the parser")
        out = self.spawn(
            "--as", "w1", "--harness", "cmd", "--model", "fake-1", "--group", "api",
            "--role", "worker", "--brief", brief, "--from", "coord",
        )
        self.assertEqual(out["harness"], "cmd")
        self.wait_for(lambda: self.member().get("turns") == "1")
        member = self.member()
        self.assertEqual((member["harness"], member["model"], member["role"]), ("cmd", "fake-1", "worker"))
        self.assertIn("build the parser", self.seat_calls()[0]["prompt"])
        self.assertEqual(self.r.smembers(store.k_groups(self.room, "w1")), {"api"})

    def test_spawn_picks_seat_by_role_band_and_headroom(self):
        picker = os.path.join(self.tmp, "picker.py")
        with open(picker, "w") as f:
            f.write('import json\nprint(json.dumps({"cmd": 12.5, "other": 3}))\n')
        config = (
            "seats:\n  cmd:\n    kind: cmd\n  other:\n    kind: cmd\n"
            "models:\n  - {model: smart-1, index: 55, seats: [other, cmd], effort: high}\n"
            "  - {model: dull-1, index: 30, seats: [cmd]}\n"
            "roles:\n  worker: {min: 50, max: 60}\n"
            f"picker: [{sys.executable}, {picker}]\n"
        )
        out = self.spawn("--as", "w2", config=config)
        self.assertEqual((out["harness"], out["model"]), ("cmd", "smart-1"))
        self.wait_for(lambda: self.member("w2").get("state") == "waiting")

    def spread_config(self):
        picker = os.path.join(self.tmp, "picker.py")
        with open(picker, "w") as f:
            f.write('import json, time\ntime.sleep(0.5)\nprint(json.dumps({"cmd": 20, "other": 15}))\n')
        return (
            "seats:\n  cmd:\n    kind: cmd\n  other:\n    kind: cmd\n"
            "models:\n  - {model: smart-1, index: 55, seats: [cmd, other]}\n"
            "roles:\n  worker: {min: 50, max: 60}\n"
            f"spread_penalty: 10\npicker: [{sys.executable}, {picker}]\n"
        )

    def test_spawns_in_one_room_spread_across_seats(self):
        config = self.spread_config()
        first = self.spawn("--as", "w1", config=config)
        second = self.spawn("--as", "w2", config=config)
        self.assertEqual((first["harness"], second["harness"]), ("cmd", "other"))

    def test_concurrent_spawns_still_spread(self):
        path = os.path.join(self.tmp, "swarm.yaml")
        with open(path, "w") as f:
            f.write(self.spread_config())
        env = dict(self.env, SWARM_CONFIG=path)
        procs = [
            subprocess.Popen(
                [sys.executable, "-m", "swarm", "--url", URL, "--room", self.room, "spawn", "--as", agent],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=env,
            )
            for agent in ("w1", "w2")
        ]
        outs = [json.loads(p.communicate(timeout=60)[0]) for p in procs]
        self.pids = [o["pid"] for o in outs]
        self.assertEqual(sorted(o["harness"] for o in outs), ["cmd", "other"])

    def test_respawn_keeps_its_seat_instead_of_counting_itself(self):
        config = self.spread_config()
        first = self.spawn("--as", "w1", config=config)
        os.kill(first["pid"], signal.SIGKILL)
        self.wait_for(lambda: subprocess.run(["kill", "-0", str(first["pid"])], capture_output=True).returncode)
        again = self.spawn("--as", "w1", config=config)
        self.assertEqual((first["harness"], again["harness"]), ("cmd", "cmd"))

    def test_spawn_refuses_a_member_that_is_running(self):
        self.spawn("--as", "w1", "--harness", "cmd")
        self.wait_for(lambda: self.member().get("state") == "waiting")
        cursor = self.r.get(store.k_cursor(self.room, "w1"))
        res = subprocess.run(
            [sys.executable, "-m", "swarm", "--url", URL, "--room", self.room, "spawn", "--as", "w1", "--harness", "cmd"],
            capture_output=True,
            text=True,
            env=self.env,
            timeout=30,
        )
        self.assertEqual(res.returncode, 3)
        self.assertIn("already running", res.stderr)
        self.assertEqual(self.r.get(store.k_cursor(self.room, "w1")), cursor)

    def test_respawn_after_the_worker_died_keeps_the_cursor(self):
        out = self.spawn("--as", "w1", "--harness", "cmd")
        self.swarm("send", "--from", "coord", "--to", "w1", "first")
        first_id = self.r.xrevrange(store.k_stream(self.room), count=1)[0][0]
        self.wait_for(lambda: stream_order(self.r.get(store.k_cursor(self.room, "w1"))) >= stream_order(first_id))
        os.kill(out["pid"], signal.SIGKILL)
        self.wait_for(lambda: subprocess.run(["kill", "-0", str(out["pid"])], capture_output=True).returncode)
        self.spawn("--as", "w1", "--harness", "cmd")
        self.swarm("send", "--from", "coord", "--to", "w1", "second")
        self.wait_for(lambda: self.member().get("turns") == "2")
        self.assertEqual(len(self.seat_calls()), 2)
        self.assertIn("second", self.seat_calls()[1]["prompt"])
        self.assertNotIn("\nfirst\n", self.seat_calls()[1]["prompt"])

    def test_worker_killed_mid_wake_gets_the_post_again_on_respawn(self):
        out = self.spawn("--as", "w1", "--harness", "cmd")
        self.swarm("send", "--from", "coord", "--to", "w1", "SLEEP then work")
        self.wait_for(lambda: len(self.seat_calls()) == 1)
        os.kill(out["pid"], signal.SIGKILL)
        self.wait_for(lambda: subprocess.run(["kill", "-0", str(out["pid"])], capture_output=True).returncode)
        self.spawn("--as", "w1", "--harness", "cmd")
        self.wait_for(lambda: len(self.seat_calls()) == 2, timeout=40)
        self.assertIn("SLEEP then work", self.seat_calls()[1]["prompt"])

    @unittest.skipUnless(sys.platform == "darwin" and shutil.which("sandbox-exec"), "macOS Seatbelt only")
    def test_spawn_refuses_inside_a_sandbox_before_joining(self):
        res = subprocess.run(
            ["sandbox-exec", "-p", "(version 1)(allow default)", sys.executable, "-m", "swarm",
             "--url", URL, "--room", self.room, "spawn", "--as", "w1", "--harness", "cmd"],
            capture_output=True,
            text=True,
            env=self.env,
            timeout=30,
        )
        self.assertEqual(res.returncode, 4, res.stderr)
        self.assertIn("sandbox", res.stderr)
        self.assertFalse(self.r.exists(store.k_member(self.room, "w1")))
        self.assertEqual(self.r.xlen(store.k_stream(self.room)), 0)

    def test_spawn_rejects_unknown_seat(self):
        res = subprocess.run(
            [sys.executable, "-m", "swarm", "--url", URL, "--room", self.room, "spawn", "--as", "w3", "--harness", "nope"],
            capture_output=True,
            text=True,
            env=self.env,
        )
        self.assertNotEqual(res.returncode, 0)
        self.assertIn("no seat 'nope'", res.stderr)

    def test_stop_retires_worker_and_respawn_recovers_cleanly(self):
        first = self.spawn("--as", "w1", "--harness", "cmd")
        self.wait_for(lambda: self.member("w1").get("state") == "waiting")
        res = self.swarm("stop", "w1", "taking a break")
        self.assertEqual(res.returncode, 0, res.stderr)
        self.wait_for(lambda: subprocess.run(["kill", "-0", str(first["pid"])], capture_output=True).returncode)
        self.assertEqual(self.member("w1").get("state"), "retired")
        self.spawn("--as", "w1", "--harness", "cmd")
        self.wait_for(lambda: self.member("w1").get("state") == "waiting")
        self.swarm("send", "--from", "coord", "--to", "w1", "back to work")
        self.wait_for(lambda: self.member("w1").get("turns") == "1")
        self.assertIn("back to work", self.seat_calls()[0]["prompt"])


class SwarmSpawnArgsTest(unittest.TestCase):
    def test_spawn_requires_a_room(self):
        dead_redis = "redis://127.0.0.1:1/0"
        pypath = os.pathsep.join(p for p in (SRC, os.environ.get("PYTHONPATH", "")) if p)
        res = subprocess.run(
            [sys.executable, "-m", "swarm", "--url", dead_redis, "spawn", "--as", "w4"],
            capture_output=True,
            text=True,
            env=dict(os.environ, PYTHONPATH=pypath, SWARM_ROOM="main"),
            timeout=30,
        )
        self.assertEqual(res.returncode, 2)
        self.assertIn("--room", res.stderr)


if __name__ == "__main__":
    unittest.main()
