"""Exercise multi-agent workflows through the CLI and a shared Redis DB 15."""
import json
import subprocess
import sys
import unittest
from decimal import Decimal

from redis.connection import parse_url

try:
    from tests.test_worker_e2e import URL, SwarmE2E, redis_up
except ModuleNotFoundError:
    from test_worker_e2e import URL, SwarmE2E, redis_up


@unittest.skipUnless(parse_url(URL).get("db") == 15 and redis_up(), "requires local Redis DB 15")
class MultiAgentE2E(SwarmE2E):
    def setUp(self):
        super().setUp()
        self.env.update(
            SWARM_KEY_PREFIX=f"pfxa-{self.room}",
            SWARM_ARCHIVE_IDLE_HOURS="0",
            SWARM_ARCHIVE_RETAIN_DAYS="7",
        )

    def room_keys(self):
        # Match the room as a whole key segment, including inside archived keys.
        return set(self.r.scan_iter(match=f"*:{self.room}")) | set(
            self.r.scan_iter(match=f"*:{self.room}:*")
        )

    def tearDown(self):
        try:
            keys = self.room_keys()
            if keys:
                self.r.delete(*keys)
            self.assertEqual(self.room_keys(), set())
        finally:
            super().tearDown()

    def cli(self, *args, prefix=None, expected=0):
        env = self.env if prefix is None else dict(self.env, SWARM_KEY_PREFIX=prefix)
        result = subprocess.run(
            [sys.executable, "-m", "swarm", "--url", URL, *args],
            capture_output=True,
            text=True,
            env=env,
            timeout=10,
        )
        self.assertEqual(result.returncode, expected, f"{args}: {result.stdout}{result.stderr}")
        return result

    def rooms(self, *args, prefix=None):
        return json.loads(self.cli("rooms", "--json", *args, prefix=prefix).stdout)

    def roster(self):
        return {row["agent"]: row for row in json.loads(self.cli("roster", "--json").stdout)}

    def send(self, sender, to, body, *args, prefix=None):
        result = self.cli("send", "--json", "--from", sender, "--to", to, *args, body, prefix=prefix)
        return json.loads(result.stdout)["id"]

    def poll(self, agent, *, expected=0):
        result = self.cli("poll", "--as", agent, "--json", expected=expected)
        return [json.loads(line) for line in result.stdout.splitlines()]

    def test_discover_room_and_exchange_handoff(self):
        self.cli("title", "API handoff")
        self.cli("join", "--as", "lead", "--role", "coordinator", "--group", "leads")
        self.cli("state", "--as", "lead", "working", "--doing", "Drafting the brief")
        brief_id = self.send("lead", "@builder", "Implement the API", "--kind", "brief")

        room, = self.rooms()
        self.assertEqual((room["room"], room["title"], room["messages"], room["present"]),
                         (self.room, "API handoff", 1, 1))
        roster = self.roster()
        self.assertEqual(set(roster), {"lead"})
        lead = roster["lead"]
        self.assertEqual((lead["role"], lead["groups"], lead["present"], lead["state"], lead["doing"]),
                         ("coordinator", ["leads"], True, "working", "Drafting the brief"))

        self.cli("--room", room["room"], "join", "--as", "builder", "--role", "worker", "--replay")
        brief, = self.poll("builder")
        self.assertEqual((brief["id"], brief["from"], brief["to"], brief["kind"], brief["body"]),
                         (brief_id, "lead", "@builder", "brief", "Implement the API"))
        self.assertEqual(self.poll("builder", expected=10), [])

        self.cli("state", "--as", "builder", "working", "--doing", "Implementing the API")
        roster = self.roster()
        self.assertEqual(set(roster), {"lead", "builder"})
        builder = roster["builder"]
        self.assertEqual((builder["role"], builder["present"], builder["state"], builder["doing"]),
                         ("worker", True, "working", "Implementing the API"))

        reply_id = self.send("builder", "@leads", "Ready for review", "--reply-to", brief_id)
        reply, = self.poll("lead")
        self.assertEqual((reply["id"], reply["from"], reply["to"], reply["reply_to"], reply["body"]),
                         (reply_id, "builder", "@leads", brief_id, "Ready for review"))
        for state, doing in (("waiting", "Waiting for review"), ("retired", "Handoff complete")):
            with self.subTest(state=state):
                self.cli("state", "--as", "builder", state, "--doing", doing)
                builder = self.roster()["builder"]
                self.assertEqual((builder["state"], builder["doing"]), (state, doing))

    def test_group_fanout_and_replay_skip_only_old_stop_posts(self):
        self.cli("join", "--as", "lead", "--role", "coordinator")
        groups = {"backend": ("api", "db"), "frontend": ("web", "mobile")}
        for group, agents in groups.items():
            for agent in agents:
                self.cli("join", "--as", agent, "--role", "worker", "--group", group)

        briefs = {group: self.send("lead", f"@{group}", f"Brief for {group}") for group in groups}
        broadcast = self.send("lead", "all", "Shared milestone")
        for group, agents in groups.items():
            for agent in agents:
                with self.subTest(agent=agent):
                    self.assertEqual([post["id"] for post in self.poll(agent)], [briefs[group], broadcast])

        self.send("lead", "all", "__SHUTDOWN__")
        self.send("lead", "all", "Previous swarm finished", "--kind", "done")
        followup = self.send("lead", "@backend", "New backend work")
        self.cli("join", "--as", "late", "--role", "worker", "--replay", "--group", "backend")
        self.assertEqual([post["id"] for post in self.poll("late")], [briefs["backend"], broadcast, followup])

        done = self.send("lead", "all", "Current swarm finished", "--kind", "done")
        shutdown = self.send("lead", "all", "__SHUTDOWN__")
        self.assertEqual([post["id"] for post in self.poll("late", expected=11)], [done, shutdown])

    def test_usage_accumulates_across_turns_for_each_agent(self):
        self.cli("join", "--as", "builder")
        self.cli("join", "--as", "reviewer")
        self.cli("usage", "--as", "builder", "--tokens-in", "100", "--tokens-cached", "900",
                 "--tokens-out", "10", "--cost", "0.125")
        self.cli("usage", "--as", "reviewer", "--tokens-in", "5", "--tokens-cached", "8",
                 "--tokens-out", "2", "--cost", "0.05")
        self.cli("usage", "--as", "builder", "--tokens-in", "40", "--tokens-cached", "60",
                 "--tokens-out", "8", "--cost", "0.25")
        self.cli("usage", "--as", "builder", "--tokens-out", "1")

        roster = self.roster()
        fields = ("tokens_in", "tokens_cached", "tokens_out", "turns")
        for agent, counts, cost in (("builder", (140, 960, 19, 3), "0.375"),
                                    ("reviewer", (5, 8, 2, 1), "0.05")):
            with self.subTest(agent=agent):
                member = roster[agent]
                self.assertEqual(tuple(int(member[field]) for field in fields), counts)
                self.assertEqual(Decimal(member["cost_usd"]), Decimal(cost))

    def test_prefix_isolates_rooms_archive_unarchive_and_clear(self):
        other = f"pfxb-{self.room}"
        self.cli("title", "Namespace A")
        self.send("lead", "all", "Message A")

        self.assertEqual(self.rooms(prefix=other), [])
        for command in ("archive", "clear"):
            with self.subTest(command=command):
                refused = self.cli(command, "--room", self.room, "--force", prefix=other, expected=1)
                self.assertIn(f"no room {self.room}", refused.stderr)

        self.cli("title", "Namespace B", prefix=other)
        self.send("lead", "all", "Message B", prefix=other)
        for prefix, title in ((None, "Namespace A"), (other, "Namespace B")):
            with self.subTest(prefix=prefix):
                room, = self.rooms(prefix=prefix)
                self.assertEqual((room["room"], room["title"], room["messages"], room["present"]),
                                 (self.room, title, 1, 1))

        archived = self.cli("archive", "--room", self.room, "--force", "--json")
        archive_id = json.loads(archived.stdout)["archive_id"]
        self.assertEqual(self.rooms(), [])
        self.assertEqual([row["archive_id"] for row in self.rooms("--archived")], [archive_id])
        self.assertEqual(self.rooms("--archived", prefix=other), [])
        self.assertEqual([row["title"] for row in self.rooms(prefix=other)], ["Namespace B"])
        refused = self.cli("unarchive", archive_id, prefix=other, expected=1)
        self.assertIn(f"no archive {archive_id}", refused.stderr)

        self.cli("unarchive", archive_id)
        restored, = self.cli("history", "--json").stdout.splitlines()
        self.assertEqual(json.loads(restored)["body"], "Message A")
        self.assertEqual(self.rooms("--archived"), [])
        self.cli("clear", "--room", self.room, "--force")
        self.assertEqual(self.rooms(), [])
        self.assertEqual(self.cli("title").stdout.strip(), self.room)
        surviving, = self.cli("history", "--json", prefix=other).stdout.splitlines()
        self.assertEqual(json.loads(surviving)["body"], "Message B")
        self.assertEqual(self.cli("title", prefix=other).stdout.strip(), "Namespace B")

    def test_invalid_prefix_exits_two_without_writing_keys(self):
        result = self.cli("send", "--from", "lead", "must not be written", prefix=f"bad:{self.room}", expected=2)
        self.assertIn("SWARM_KEY_PREFIX", result.stderr)
        self.assertEqual(self.room_keys(), set())


if __name__ == "__main__":
    unittest.main()
