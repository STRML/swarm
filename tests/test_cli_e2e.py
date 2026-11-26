"""End-to-end: the messaging and inspection `swarm` CLI as subprocesses against real local Redis (db 15 by default)."""
import json
import os
import queue
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

DEADLINE = 20  # seconds; only a broken CLI ever gets near it
DEAD_URL = "redis://127.0.0.1:1/15"


class LineStream:
    """Lines of a live process's stdout, readable with a deadline while the process keeps running."""

    def __init__(self, proc):
        self.lines = queue.Queue()
        self.reader = threading.Thread(target=self._pump, args=(proc.stdout,), daemon=True)
        self.reader.start()

    def _pump(self, pipe):
        with pipe:
            for line in pipe:
                self.lines.put(line.rstrip("\n"))

    def take(self, count):
        end = time.monotonic() + DEADLINE
        got = []
        while len(got) < count:
            try:
                got.append(self.lines.get(timeout=max(0, end - time.monotonic())))
            except queue.Empty:
                raise AssertionError(f"expected {count} lines, got {len(got)} before the deadline: {got}") from None
        return got


@unittest.skipUnless(redis_up(), "local Redis not reachable")
class CliE2E(SwarmE2E):
    def setUp(self):
        super().setUp()
        # An inherited PYTHONUNBUFFERED would hide a missing flush in `watch`.
        self.env.pop("PYTHONUNBUFFERED", None)
        self.env["PYTHONIOENCODING"] = "utf-8"
        self.streams = []

    def tearDown(self):
        super().tearDown()
        for stream in self.streams:
            stream.reader.join(timeout=5)
        self.assertEqual(list(self.r.scan_iter(match=f"*{self.room}*")), [], "leaked keys")

    # -- process helpers ----------------------------------------------------

    def cli(self, *argv, stdin="", **env):
        return subprocess.run(
            [sys.executable, "-m", "swarm", *argv],
            input=stdin,
            capture_output=True,
            encoding="utf-8",
            env={**self.env, **env},
            timeout=DEADLINE,
        )

    def run_swarm(self, *args, stdin="", rc=0):
        res = self.cli("--url", URL, "--room", self.room, *args, stdin=stdin)
        self.assertEqual(res.returncode, rc, f"swarm {' '.join(args)}\n{res.stdout}{res.stderr}")
        return res

    def start(self, *args):
        with open(os.path.join(self.tmp, f"proc-{len(self.procs)}.err"), "w") as err:
            proc = subprocess.Popen(
                [sys.executable, "-m", "swarm", "--url", URL, "--room", self.room, *args],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=err,
                encoding="utf-8",
                env=self.env,
            )
        self.procs.append(proc)
        return proc

    def follow(self, *args):
        proc = self.start(*args)
        stream = LineStream(proc)
        self.streams.append(stream)
        return proc, stream

    def finish(self, proc):
        out, _ = proc.communicate(timeout=DEADLINE)
        return proc.returncode, out

    def wait_until(self, predicate, what):
        end = time.monotonic() + DEADLINE
        while not predicate():
            self.assertLess(time.monotonic(), end, f"timed out waiting for {what}")
            time.sleep(0.01)

    # -- room helpers -------------------------------------------------------

    def send(self, body, *flags, frm="boss"):
        res = self.run_swarm("--json", "send", "--from", frm, *flags, body)
        return json.loads(res.stdout)["id"]

    def join(self, agent, *flags):
        self.run_swarm("join", "--as", agent, *flags)

    def seed(self, *bodies, topic="", to="all"):
        """Append messages straight to the stream, atomically and without touching presence or cursors."""
        pipe = self.r.pipeline(transaction=True)
        for body in bodies:
            pipe.xadd(store.k_stream(self.room), store.make_fields("boss", to, body, topic=topic))
        return pipe.execute()

    def json_lines(self, *args, rc=0):
        res = self.run_swarm("--json", *args, rc=rc)
        return [json.loads(line) for line in res.stdout.splitlines()]

    def ids(self, *args, rc=0):
        return [m["id"] for m in self.json_lines(*args, rc=rc)]

    def stored(self, room=None):
        return [fields for _, fields in self.r.xrange(store.k_stream(room or self.room))]

    def cursor(self, agent):
        return self.r.get(store.k_cursor(self.room, agent))

    def present(self, agent):
        return bool(self.r.exists(store.k_presence(self.room, agent)))

    def side_state(self):
        """Every key of the room except the stream, with its value: cursors, presence, members, groups."""
        read = {"string": self.r.get, "hash": self.r.hgetall, "set": self.r.smembers}
        return {
            key: read[self.r.type(key)](key)
            for key in self.r.scan_iter(match=f"*{self.room}*")
            if key != store.k_stream(self.room)
        }


class SendE2E(CliE2E):
    AWKWARD = "line one\n  $(rm -rf nope) `ticks` \"quotes\" 'single' \\n literal\nünïcode ✓"

    def test_dash_body_reads_stdin_byte_for_byte_apart_from_outer_whitespace(self):
        res = self.run_swarm("send", "--from", "a", "-", stdin=f"\n{self.AWKWARD}\n\n")
        [(msg_id, fields)] = self.r.xrange(store.k_stream(self.room))
        self.assertEqual(fields, store.make_fields("a", "all", self.AWKWARD))
        self.assertEqual(res.stdout, f"sent {msg_id} to room={self.room} to=all\n")
        self.assertTrue(self.present("a"))

    def test_omitted_body_reads_piped_stdin(self):
        self.run_swarm("send", "--from", "a", "--to", "b", stdin="piped body\n")
        self.assertEqual(self.stored(), [store.make_fields("a", "b", "piped body")])

    def test_argument_body_wins_over_piped_stdin(self):
        self.run_swarm("send", "--from", "a", "from argv", stdin="from stdin")
        self.assertEqual([m["body"] for m in self.stored()], ["from argv"])

    def test_topic_reply_to_and_question_kind_reach_readers(self):
        parent = self.send("parent")
        child = self.send("why?", "--topic", "api", "--reply-to", parent, "--kind", "question", "--to", "dev,qa")
        self.assertEqual(
            self.json_lines("history")[1],
            {"id": child, "from": "boss", "to": "dev,qa", "topic": "api",
             "reply_to": parent, "kind": "question", "body": "why?"},
        )
        plain = self.run_swarm("history").stdout
        self.assertIn(f"{child}  boss -> dev,qa [api] (re {parent})\n  why?\n", plain)

    def test_unknown_mention_warns_on_stderr_and_still_delivers(self):
        res = self.run_swarm("send", "--from", "a", "--to", "@ghost", "hello")
        self.assertIn(f"warning: no agent or group '@ghost' in room={self.room}", res.stderr)
        self.assertNotIn("warning", res.stdout)
        self.join("ghost", "--replay")
        self.assertEqual([m["body"] for m in self.json_lines("poll", "--as", "ghost")], ["hello"])

    def test_known_agent_and_group_mentions_do_not_warn(self):
        self.join("dev", "--group", "backend")
        res = self.run_swarm("send", "--from", "a", "--to", "@dev,@backend,@nobody", "hi")
        self.assertEqual(res.stderr.count("warning"), 1, res.stderr)
        self.assertIn("'@nobody'", res.stderr)

    def test_empty_body_exits_2_and_writes_nothing(self):
        cases = {
            "empty argument": (("",), ""),
            "dash with empty stdin": (("-",), ""),
            "whitespace-only stdin": (("-",), "   \n\n"),
            "omitted with empty stdin": ((), ""),
        }
        for name, (body_args, stdin) in cases.items():
            with self.subTest(name):
                res = self.run_swarm("send", "--from", "a", *body_args, stdin=stdin, rc=2)
                self.assertIn("empty body", res.stderr)
                self.assertEqual(res.stdout, "")
                self.assertEqual(list(self.r.scan_iter(match=f"*{self.room}*")), [])

    def test_worker_reserved_kinds_exit_2_and_write_nothing(self):
        for kind in store.WORKER_KINDS:
            with self.subTest(kind):
                res = self.run_swarm("send", "--from", "a", "--kind", kind, "x", rc=2)
                self.assertIn("reserved", res.stderr)
                self.assertEqual(self.stored(), [])


class PollWaitE2E(CliE2E):
    def test_poll_delivers_once_then_exits_10(self):
        self.join("w1")
        sent = self.send("work", "--to", "w1")
        self.assertEqual(self.ids("poll", "--as", "w1"), [sent])
        self.assertEqual(self.cursor("w1"), sent)
        self.assertEqual(self.run_swarm("poll", "--as", "w1", rc=10).stdout, "(no new messages)\n")
        self.assertEqual(self.run_swarm("--json", "poll", "--as", "w1", rc=10).stdout, "")

    def test_poll_topic_narrows_delivery_and_consumes_the_rest(self):
        self.join("w1")
        self.send("no topic")
        wanted = self.send("wanted", "--topic", "api")
        last = self.send("other", "--topic", "ui")
        self.assertEqual(self.ids("poll", "--as", "w1", "--topic", "api"), [wanted])
        self.assertEqual(self.cursor("w1"), last)
        self.run_swarm("poll", "--as", "w1", rc=10)

    def test_wait_blocks_until_a_second_process_sends(self):
        waiter = self.start("--json", "wait", "--as", "w1", "--timeout", "60")
        self.wait_until(lambda: self.present("w1"), "wait to register presence")
        noise = self.send("not yours", "--to", "someone-else")
        self.wait_until(lambda: self.cursor("w1") == noise, "wait to read past the unrelated post")
        self.assertIsNone(waiter.poll(), "wait returned for a post addressed to someone else")

        wanted = self.send("yours", "--to", "w1")
        rc, out = self.finish(waiter)
        self.assertEqual(rc, 0)
        self.assertEqual([json.loads(line)["id"] for line in out.splitlines()], [wanted])
        self.assertEqual(self.cursor("w1"), wanted)

    def test_wait_filters_keep_it_blocked_until_a_match(self):
        parent, other_parent = self.seed("parent", "other parent", to="nobody")
        cases = {
            "topic": (("--topic", "api"), ("--topic", "ui"), ("--topic", "api")),
            "reply-to": (("--reply-to", parent), ("--reply-to", other_parent), ("--reply-to", parent)),
            "both": (
                ("--topic", "api", "--reply-to", parent),
                ("--topic", "api", "--reply-to", other_parent),
                ("--topic", "api", "--reply-to", parent),
            ),
        }
        for name, (filters, miss, hit) in cases.items():
            with self.subTest(name):
                agent = f"w-{name}"
                self.join(agent)
                waiter = self.start("--json", "wait", "--as", agent, "--timeout", "60", *filters)
                skipped = self.send("miss", *miss)
                self.wait_until(lambda: self.cursor(agent) == skipped, "wait to read past the filtered post")
                self.assertIsNone(waiter.poll(), "wait returned for a filtered post")
                wanted = self.send("hit", *hit)
                rc, out = self.finish(waiter)
                self.assertEqual(rc, 0)
                self.assertEqual([json.loads(line)["id"] for line in out.splitlines()], [wanted])

    def test_wait_timeout_exits_10_after_blocking_for_the_timeout(self):
        self.join("w1")
        started = time.monotonic()
        res = self.run_swarm("wait", "--as", "w1", "--timeout", "0.8", rc=10)
        self.assertGreaterEqual(time.monotonic() - started, 0.8)
        self.assertEqual(res.stdout, "(timeout, no messages)\n")
        self.assertEqual(self.run_swarm("--json", "wait", "--as", "w1", "--timeout", "0.2", rc=10).stdout, "")

    def test_shutdown_exits_11_through_topic_and_reply_to_filters(self):
        commands = {
            "poll --topic": ("poll", "--topic", "api"),
            "wait --topic": ("wait", "--timeout", "10", "--topic", "api"),
            "wait --reply-to": ("wait", "--timeout", "10", "--reply-to", "1-0"),
            "wait both": ("wait", "--timeout", "10", "--topic", "api", "--reply-to", "1-0"),
        }
        for index, name in enumerate(commands):
            self.join(f"w{index}")
        stop = self.send(store.SHUTDOWN, "--topic", "ui", "--reply-to", "2-0")
        for index, (name, command) in enumerate(commands.items()):
            with self.subTest(name):
                self.assertEqual(self.ids(*command, "--as", f"w{index}", rc=11), [stop])

    def test_shutdown_wakes_a_blocked_filtered_wait(self):
        waiter = self.start("wait", "--as", "w1", "--timeout", "60", "--topic", "api")
        self.wait_until(lambda: self.present("w1"), "wait to register presence")
        self.send(store.SHUTDOWN)
        rc, out = self.finish(waiter)
        self.assertEqual(rc, 11)
        self.assertIn(store.SHUTDOWN, out)

    def test_shutdown_for_another_agent_is_not_delivered(self):
        self.join("w1")
        self.send(store.SHUTDOWN, "--to", "w2")
        self.run_swarm("poll", "--as", "w1", rc=10)


class WatchE2E(CliE2E):
    def test_plain_watch_streams_each_batch_while_still_running(self):
        [context] = self.seed("context")
        proc, out = self.follow("watch", "-n", "1")
        self.assertEqual(out.take(2), [f"{context}  boss", "  context"])
        for batch in (("one", "two", "three"), ("four",)):
            ids = self.seed(*batch)
            expected = [line for mid, body in zip(ids, batch) for line in (f"{mid}  boss", f"  {body}")]
            self.assertEqual(out.take(len(expected)), expected)
        self.assertIsNone(proc.poll())

    def test_json_watch_streams_posts_from_the_cli_and_any_recipient(self):
        [context] = self.seed("context")
        proc, out = self.follow("--json", "watch", "-n", "1")
        self.assertEqual(json.loads(out.take(1)[0])["id"], context)
        private = self.send("for dev only", "--to", "dev")
        self.assertEqual(
            json.loads(out.take(1)[0]),
            {"id": private, "from": "boss", "to": "dev", "topic": "", "reply_to": "",
             "kind": "msg", "body": "for dev only"},
        )
        self.assertIsNone(proc.poll())

    def test_watch_topic_filters_context_and_live_posts(self):
        [older] = self.seed("older api", topic="api")
        self.seed("newer ui", topic="ui")
        proc, out = self.follow("--json", "watch", "-n", "1", "--topic", "api")
        self.assertEqual(json.loads(out.take(1)[0])["id"], older)
        self.seed("live ui", topic="ui")
        self.seed("live untagged")
        [live] = self.seed("live api", topic="api")
        self.assertEqual(json.loads(out.take(1)[0])["id"], live)
        self.assertIsNone(proc.poll())

    def test_watch_leaves_cursors_presence_and_roster_untouched(self):
        self.join("dev", "--group", "backend")
        self.join("away")
        self.r.delete(store.k_presence(self.room, "away"))
        self.seed("context")
        before = self.side_state()

        proc, out = self.follow("--json", "watch", "-n", "1")
        out.take(1)
        self.seed("directed", to="@dev")
        out.take(1)
        self.assertEqual(self.side_state(), before)
        proc.kill()
        proc.wait(timeout=DEADLINE)
        self.assertEqual(self.side_state(), before)


class InspectE2E(CliE2E):
    def test_thread_rebuilds_the_whole_branching_tree_from_any_member(self):
        root = self.send("root")
        left = self.send("left", "--reply-to", root)
        stranger = self.send("unrelated root")
        right = self.send("right", "--reply-to", root)
        left_leaf = self.send("left leaf", "--reply-to", left)
        self.send("unrelated reply", "--reply-to", stranger)
        right_leaf = self.send("right leaf", "--reply-to", right)
        deepest = self.send("deepest", "--reply-to", left_leaf)
        tree = [root, left, right, left_leaf, right_leaf, deepest]
        for member in tree:
            with self.subTest(member=member):
                self.assertEqual(self.ids("thread", member), tree)

    def test_thread_with_a_missing_parent_starts_at_the_orphan(self):
        orphan = self.send("orphan", "--reply-to", "1-0")
        child = self.send("child", "--reply-to", orphan)
        self.send("sibling orphan", "--reply-to", "1-0")
        self.assertEqual(self.ids("thread", child), [orphan, child])

    def test_thread_unknown_id_exits_1(self):
        self.send("root")
        res = self.run_swarm("thread", "1-0", rc=1)
        self.assertIn(f"message 1-0 not in room {self.room}", res.stderr)
        self.assertEqual(res.stdout, "")

    def test_inbox_peeks_directed_posts_and_questions_without_moving_the_cursor(self):
        self.join("dev", "--group", "backend")
        direct = self.send("direct", "--to", "dev")
        self.send("broadcast chatter")
        group = self.send("group", "--to", "@backend", "--topic", "api")
        shared = self.send("shared", "--to", "qa,dev")
        self.send("for qa", "--to", "qa")
        question = self.send("anyone?", "--kind", "question")
        self.send("qa only?", "--to", "qa", "--kind", "question")
        self.send("my own", "--to", "@backend", frm="dev")
        cursor = self.cursor("dev")
        expected = [direct, group, shared, question]

        self.assertEqual(self.ids("inbox", "--as", "dev"), expected)
        self.assertEqual(
            self.run_swarm("inbox", "--as", "dev").stdout.splitlines(),
            [
                f"@ {direct}  boss: direct",
                f"@ {group}  boss [api]: group",
                f"@ {shared}  boss: shared",
                f"Q {question}  boss: anyone?",
            ],
        )
        self.assertEqual(self.cursor("dev"), cursor)
        polled = self.json_lines("poll", "--as", "dev")
        self.assertEqual(
            [m["body"] for m in polled], ["direct", "broadcast chatter", "group", "shared", "anyone?"]
        )

    def test_inbox_is_empty_once_polled(self):
        self.join("dev")
        self.send("direct", "--to", "dev")
        self.run_swarm("poll", "--as", "dev")
        self.assertIn("inbox empty", self.run_swarm("inbox", "--as", "dev").stdout)
        self.assertEqual(self.run_swarm("--json", "inbox", "--as", "dev").stdout, "")

    def test_tail_and_history_read_opposite_ends_without_side_effects(self):
        self.join("dev")
        ids = self.seed("m0", "m1", "m2", "m3", "m4")
        before = self.side_state()
        self.assertEqual(self.ids("tail", "-n", "2"), ids[-2:])
        self.assertEqual(self.ids("tail"), ids)
        self.assertEqual(self.ids("history", "-n", "2"), ids[:2])
        self.assertEqual(self.ids("history"), ids)
        self.assertEqual(
            self.run_swarm("tail", "-n", "1").stdout, f"{ids[-1]}  boss\n  m4\n"
        )
        self.assertEqual(self.side_state(), before)

    def test_agents_lists_only_present_members_of_this_room(self):
        self.assertEqual(
            self.run_swarm("agents").stdout, f"(no agents present in room={self.room})\n"
        )
        for agent in ("zed", "amy", "gone"):
            self.join(agent)
        self.run_swarm("--room", f"{self.room}-b", "join", "--as", "neighbour")
        self.r.delete(store.k_presence(self.room, "gone"))  # what TTL expiry does

        rows = self.json_lines("agents")[0]
        self.assertEqual([row["agent"] for row in rows], ["amy", "zed"])
        for row in rows:
            self.assertEqual(row["last_seen"], self.r.get(store.k_presence(self.room, row["agent"])))
            self.assertTrue(0 < self.r.ttl(store.k_presence(self.room, row["agent"])) <= store.PRESENCE_TTL)
        self.assertEqual(
            self.run_swarm("agents").stdout.splitlines(),
            [f"{row['agent']}\tlast_seen={row['last_seen']}" for row in rows],
        )


class PruneE2E(CliE2E):
    def bodies(self):
        return [m["body"] for m in self.stored()]

    def fill(self):
        return self.seed(*(f"m{i}" for i in range(10)))

    def test_prune_refuses_for_a_lagging_cursor_then_force_trims_to_the_keep_window(self):
        self.join("lag")
        self.join("caught")
        self.fill()
        self.run_swarm("poll", "--as", "caught")
        before = self.side_state()

        refused = self.run_swarm("prune", "--keep", "3", rc=1)
        self.assertIn("REFUSING - 1 agent cursor(s)", refused.stderr)
        self.assertIn("lag@0-0", refused.stderr)
        self.assertNotIn("caught@", refused.stderr)
        self.assertEqual(refused.stdout, "")
        self.assertEqual(self.bodies(), [f"m{i}" for i in range(10)])

        forced = self.run_swarm("prune", "--keep", "3", "--force")
        self.assertIn("dropping unread for 1 lagging agent(s)", forced.stderr)
        self.assertEqual(forced.stdout, f"pruned room {self.room}: removed 7, kept 3 (was 10)\n")
        self.assertEqual(self.bodies(), ["m7", "m8", "m9"])
        self.assertEqual(self.side_state(), before)
        self.assertEqual([m["body"] for m in self.json_lines("poll", "--as", "lag")], ["m7", "m8", "m9"])

    def test_prune_lag_boundary_is_the_last_message_outside_the_keep_window(self):
        ids = self.fill()
        self.join("dev")
        self.r.set(store.k_cursor(self.room, "dev"), ids[5])  # m6 is unread and would be trimmed
        self.run_swarm("prune", "--keep", "3", rc=1)
        self.assertEqual(len(self.stored()), 10)

        self.r.set(store.k_cursor(self.room, "dev"), ids[6])  # everything unread is inside the window
        res = self.run_swarm("prune", "--keep", "3")
        self.assertEqual(res.stderr, "")
        self.assertEqual(self.bodies(), ["m7", "m8", "m9"])

    def test_prune_within_keep_removes_nothing(self):
        self.join("lag")
        self.fill()
        res = self.run_swarm("prune", "--keep", "10")
        self.assertIn("10 messages <= keep 10; nothing to trim", res.stdout)
        self.assertEqual(len(self.stored()), 10)

    def test_prune_rejects_keep_zero_even_with_force(self):
        self.fill()
        self.run_swarm("prune", "--keep", "0", "--force", rc=2)
        self.assertEqual(len(self.stored()), 10)


class DoctorAndFlagsE2E(CliE2E):
    def doctor(self, *argv, rc=0, **env):
        res = self.cli(*argv, **env)
        self.assertEqual(res.returncode, rc, res.stdout + res.stderr)
        return json.loads(res.stdout)

    def test_doctor_json_reports_redis_and_config_ok(self):
        report = self.doctor("--url", URL, "doctor", "--json")
        self.assertTrue(report["redis"]["ok"])
        self.assertEqual(report["redis"]["url"], URL)
        self.assertTrue(report["redis"]["version"])
        self.assertTrue(report["config"]["ok"])
        self.assertFalse(report["config"]["exists"])
        self.assertEqual(report["config"]["path"], self.env["SWARM_CONFIG"])
        self.assertEqual(report["state_dir"], self.env["SWARM_STATE_DIR"])

    def test_doctor_exits_1_when_redis_is_unreachable(self):
        report = self.doctor("--url", DEAD_URL, "doctor", "--json", rc=1)
        self.assertFalse(report["redis"]["ok"])
        self.assertTrue(report["redis"]["error"])
        self.assertTrue(report["config"]["ok"])

    def test_doctor_exits_1_when_the_config_is_invalid(self):
        path = os.path.join(self.tmp, "bad.yaml")
        with open(path, "w") as f:
            f.write("seats:\n  broken:\n    kind: nonsense\n")
        report = self.doctor("--url", URL, "doctor", "--json", rc=1, SWARM_CONFIG=path)
        self.assertTrue(report["redis"]["ok"])
        self.assertFalse(report["config"]["ok"])
        self.assertIn("broken", report["config"]["error"])

    def test_doctor_accepts_flags_before_and_after_the_subcommand(self):
        flags = ("--url", URL, "--room", self.room, "--json")
        for name, argv in (("before", (*flags, "doctor")), ("after", ("doctor", *flags))):
            with self.subTest(name):
                self.assertTrue(self.doctor(*argv, SWARM_REDIS_URL=DEAD_URL)["redis"]["ok"])

    def test_url_room_and_json_work_before_and_after_subcommands(self):
        other = f"{self.room}-b"  # differs from SWARM_ROOM, so a dropped --room is visible
        flags = ("--url", URL, "--room", other, "--json")
        placements = {
            "before": lambda command: (*flags, *command),
            "after": lambda command: (*command, *flags),
        }
        for name, place in placements.items():
            with self.subTest(name):
                sent = self.cli(*place(("send", "--from", "a", name)), SWARM_REDIS_URL=DEAD_URL)
                self.assertEqual(sent.returncode, 0, sent.stderr)
                receipt = json.loads(sent.stdout)
                self.assertEqual(receipt["room"], other)
                read = self.cli(*place(("tail", "-n", "1")), SWARM_REDIS_URL=DEAD_URL)
                self.assertEqual(read.returncode, 0, read.stderr)
                self.assertEqual(json.loads(read.stdout)["id"], receipt["id"])
        self.assertEqual([m["body"] for m in self.stored(other)], ["before", "after"])
        self.assertEqual(self.stored(), [])

    def test_room_falls_back_to_swarm_room(self):
        res = self.cli("--url", URL, "send", "--from", "a", "defaulted")
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual([m["body"] for m in self.stored()], ["defaulted"])


if __name__ == "__main__":
    unittest.main()
