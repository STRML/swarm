"""Room addressing, replay, member state, inbox, threads, pruning, and CLI parsing (in-memory fakeredis)."""
import argparse
import contextlib
import io
import json
import types
import unittest

try:
    import fakeredis
except ImportError:  # pragma: no cover
    fakeredis = None

from swarm import cli, store

ROOM = "swarm-t"


def _args(**kw):
    ns = types.SimpleNamespace(
        room=ROOM,
        room_given=True,
        json=True,
        topic=None,
        reply_to=None,
        kind="msg",
        replay=False,
        group=None,
        role=None,
        model=None,
        workspace=None,
        conversation=None,
        harness=None,
        doing=None,
    )
    ns.__dict__.update(kw)
    return ns


class RecipientsValidatorTest(unittest.TestCase):
    def test_dedupes_and_keeps_order(self):
        self.assertEqual(store.recipients("w1,w2,w1"), "w1,w2")

    def test_all_wins(self):
        self.assertEqual(store.recipients("w1,all,@g"), "all")

    def test_accepts_groups(self):
        self.assertEqual(store.recipients("@api,verifier"), "@api,verifier")

    def test_rejects_malformed(self):
        for value in ("w1,,w2", "@", "a b", "x:y", "", "@a/b"):
            with self.subTest(value=value):
                with self.assertRaises(argparse.ArgumentTypeError):
                    store.recipients(value)


class DeliverableTest(unittest.TestCase):
    def msg(self, to, frm="coord", body="hi", kind="msg", topic="", reply_to="", msg_id="100-0"):
        return {
            "id": msg_id,
            "to": to,
            "from": frm,
            "body": body,
            "topic": topic,
            "reply_to": reply_to,
            "kind": kind,
        }

    def test_single_id(self):
        self.assertTrue(store.deliverable(self.msg("w1"), "w1"))
        self.assertFalse(store.deliverable(self.msg("w1"), "w2"))

    def test_comma_list(self):
        self.assertTrue(store.deliverable(self.msg("w1,w2"), "w2"))

    def test_group_member_receives(self):
        self.assertTrue(store.deliverable(self.msg("@api"), "w1", groups={"api"}))
        self.assertFalse(store.deliverable(self.msg("@api"), "w2", groups={"ui"}))

    def test_at_name_reaches_agent_with_that_id(self):
        self.assertTrue(store.deliverable(self.msg("@w1"), "w1"))
        self.assertFalse(store.deliverable(self.msg("@w1"), "w2"))

    def test_sender_in_group_skips_own_post(self):
        self.assertFalse(store.deliverable(self.msg("@api", frm="w1"), "w1", groups={"api"}))

    def test_shutdown_bypasses_topic_and_reply_filters(self):
        m = self.msg("all", body=store.SHUTDOWN, topic="other")
        self.assertTrue(store.deliverable(m, "w1", topic="mine", reply_to="99-0"))


@unittest.skipIf(fakeredis is None, "fakeredis not installed")
class StoreRedisTest(unittest.TestCase):
    def setUp(self):
        self.r = fakeredis.FakeStrictRedis(decode_responses=True)

    def run_cmd(self, fn, **kw):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = fn(self.r, _args(**kw))
        return rc, out.getvalue(), err.getvalue()

    def send(self, frm, to, body="x", **kw):
        return self.run_cmd(cli.cmd_send, frm=frm, to=to, body=body, **kw)

    def poll(self, agent, **kw):
        _, out, _ = self.run_cmd(cli.cmd_poll, as_agent=agent, **kw)
        return [json.loads(line) for line in out.splitlines() if line.strip()]

    def test_send_to_empty_group_warns_but_sends(self):
        rc, _, err = self.send("coord", "@nobody")
        self.assertEqual(rc, 0)
        self.assertIn("@nobody", err)
        self.assertEqual(self.r.xlen(store.k_stream(ROOM)), 1)

    def test_late_joiner_to_group_gets_backlog_on_replay(self):
        self.send("coord", "@api", body="brief for api")
        self.send("coord", "w9", body="not for w1")
        self.run_cmd(cli.cmd_join, as_agent="w1", replay=True, group=["api"])
        self.assertEqual([m["body"] for m in self.poll("w1")], ["brief for api"])

    def test_replay_skips_shutdown_sent_before_join(self):
        self.send("coord", "all", body=store.SHUTDOWN)
        self.send("coord", "w1", body="new brief")
        self.run_cmd(cli.cmd_join, as_agent="w1", replay=True)
        self.send("coord", "w1", body="after join")
        self.assertEqual([m["body"] for m in self.poll("w1")], ["new brief", "after join"])

    def test_replay_skips_done_sent_before_join(self):
        self.send("coord", "all", body="old swarm over", kind="done")
        self.run_cmd(cli.cmd_join, as_agent="w1", replay=True)
        self.assertEqual(self.poll("w1"), [])

    def test_shutdown_after_replay_join_still_stops(self):
        self.run_cmd(cli.cmd_join, as_agent="w1", replay=True)
        self.send("coord", "all", body=store.SHUTDOWN)
        rc, _, _ = self.run_cmd(cli.cmd_poll, as_agent="w1")
        self.assertEqual(rc, store.RC_SHUTDOWN)

    def test_join_without_replay_starts_at_now(self):
        self.send("coord", "all", body="old")
        self.run_cmd(cli.cmd_join, as_agent="w1")
        self.assertEqual(self.poll("w1"), [])

    def test_join_group_twice_is_idempotent(self):
        self.run_cmd(cli.cmd_join, as_agent="w1", group=["api"])
        self.run_cmd(cli.cmd_join, as_agent="w1", group=["api"])
        self.assertEqual(self.r.smembers(store.k_groups(ROOM, "w1")), {"api"})

    def test_question_to_group_lands_in_member_inbox(self):
        self.run_cmd(cli.cmd_join, as_agent="w1", group=["api"])
        self.send("coord", "@api", body="which port?", kind="question")
        _, out, _ = self.run_cmd(cli.cmd_inbox, as_agent="w1")
        self.assertIn("which port?", out)

    def test_thread_reconstructs_full_reply_tree(self):
        _, o1, _ = self.send("a", "b", body="root")
        id1 = json.loads(o1)["id"]
        _, o2, _ = self.send("b", "a", body="reply", reply_to=id1)
        id2 = json.loads(o2)["id"]
        self.send("c", "all", body="unrelated")
        _, out, _ = self.run_cmd(cli.cmd_thread, id=id2)
        msgs = [json.loads(line) for line in out.splitlines() if line.strip()]
        self.assertEqual([m["body"] for m in msgs], ["root", "reply"])

    def test_join_records_member_metadata(self):
        self.run_cmd(
            cli.cmd_join,
            as_agent="w1",
            role="worker",
            model="sonnet",
            workspace="/tmp/w1",
            conversation="abc",
        )
        member = self.r.hgetall(store.k_member(ROOM, "w1"))
        self.assertEqual(member["role"], "worker")
        self.assertEqual(member["model"], "sonnet")
        self.assertEqual(member["workspace"], "/tmp/w1")
        self.assertEqual(member["conversation"], "abc")
        self.assertIn("joined", member)

    def test_state_and_usage_and_title_and_roster(self):
        self.run_cmd(cli.cmd_state, as_agent="w1", state="working", doing="writing migrations")
        self.assertEqual(self.r.hget(store.k_member(ROOM, "w1"), "doing"), "writing migrations")
        self.run_cmd(cli.cmd_usage, as_agent="w1", tokens_in=100, tokens_cached=900, tokens_out=20, cost=0.5)
        self.run_cmd(cli.cmd_usage, as_agent="w1", tokens_in=50, tokens_cached=0, tokens_out=5, cost=None)
        member = self.r.hgetall(store.k_member(ROOM, "w1"))
        self.assertEqual((member["tokens_in"], member["tokens_cached"], member["tokens_out"], member["turns"]),
                         ("150", "900", "25", "2"))
        self.assertAlmostEqual(float(member["cost_usd"]), 0.5)

        self.run_cmd(cli.cmd_title, text="Epic 62")
        _, out, _ = self.run_cmd(cli.cmd_title, text=None)
        self.assertIn("Epic 62", out)

        self.run_cmd(cli.cmd_join, as_agent="w1", role="worker", harness="codex", model="gpt-6", group=["api"])
        self.r.delete(store.k_presence(ROOM, "w1"))
        _, out, _ = self.run_cmd(cli.cmd_roster)
        roster = json.loads(out)
        self.assertEqual(roster[0]["agent"], "w1")
        self.assertEqual(roster[0]["harness"], "codex")
        self.assertFalse(roster[0]["present"])
        self.assertEqual(roster[0]["groups"], ["api"])


class ParserTest(unittest.TestCase):
    def test_state_rejects_unknown_value(self):
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                cli.build_parser().parse_args(["state", "--as", "w1", "sleeping"])

    def test_common_flags_work_before_or_after_subcommand(self):
        a = cli.build_parser().parse_args(["--room", "r1", "--json", "roster"])
        b = cli.build_parser().parse_args(["roster", "--room", "r1", "--json"])
        self.assertEqual((a.room, a.json), ("r1", True))
        self.assertEqual((b.room, b.json), ("r1", True))

    def test_join_accepts_swarm_flags(self):
        args = cli.build_parser().parse_args([
            "join", "--as", "w1", "--replay", "--group", "api", "--group", "ui",
            "--role", "worker", "--model", "m", "--workspace", "/w", "--conversation", "c",
        ])
        self.assertEqual(args.group, ["api", "ui"])
        self.assertTrue(args.replay)

    def test_bare_swarm_parses_without_subcommand(self):
        args = cli.build_parser().parse_args([])
        self.assertIsNone(args.cmd)

    def test_list_rooms_includes_presence_only_room(self):
        if fakeredis is None:
            self.skipTest("fakeredis not installed")
        r = fakeredis.FakeStrictRedis(decode_responses=True)
        store.join_member(r, "repo-alpha", "claude-1", harness="claude")
        rows = store.list_rooms(r)
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0]["room"], rows[0]["messages"], rows[0]["present"]), ("repo-alpha", 0, 1))


if __name__ == "__main__":
    unittest.main()
