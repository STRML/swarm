"""End-to-end: `--room .`, `done`, `stop`, `logs` and `init` as real subprocesses against local Redis db 15."""
import json
import os
import pty
import shutil
import signal
import subprocess
import sys
import unittest

from swarm import config as swarm_config
from swarm import store
try:
    from tests.test_cli_e2e import DEAD_URL, DEADLINE, CliE2E, LineStream
    from tests.test_worker_e2e import ROOT, SRC, URL, redis_up
except ModuleNotFoundError:
    from test_cli_e2e import DEAD_URL, DEADLINE, CliE2E, LineStream
    from test_worker_e2e import ROOT, SRC, URL, redis_up


@unittest.skipUnless(redis_up(), "local Redis not reachable")
class RoomDotE2E(CliE2E):
    def setUp(self):
        super().setUp()
        self.env = {k: v for k, v in self.env.items() if not k.startswith("GIT_")}
        self.env.update(GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1", GIT_CEILING_DIRECTORIES=self.tmp)
        self.env.pop("SWARM_ROOM")

    def git(self, cwd, *args):
        identity = ("-c", "user.name=t", "-c", "user.email=t@example.invalid")
        subprocess.run(["git", *identity, *args], cwd=cwd, env=self.env, check=True, capture_output=True)

    def directory(self, *parts):
        path = os.path.join(self.tmp, *parts)
        os.makedirs(path, exist_ok=True)
        return path

    def repo(self, name):
        path = self.directory(name)
        self.git(path, "init", "-q")
        self.git(path, "commit", "-q", "--allow-empty", "-m", "init")
        return path

    def at(self, cwd, *argv, rc=0, **env):
        res = subprocess.run(
            [sys.executable, "-m", "swarm", "--url", URL, *argv],
            cwd=cwd, input="", capture_output=True, text=True, env={**self.env, **env}, timeout=DEADLINE,
        )
        self.assertEqual(res.returncode, rc, f"swarm {' '.join(argv)}\n{res.stdout}{res.stderr}")
        return res

    def sent_room(self, cwd, *flags, **env):
        res = self.at(cwd, *flags, "send", "--from", "a", "--json", "hi", **env)
        return json.loads(res.stdout)["room"]

    def test_repo_and_its_subdirectory_resolve_to_the_repo_basename(self):
        repo = self.repo(self.room)
        for cwd in (repo, self.directory(self.room, "deep", "er")):
            with self.subTest(cwd=cwd):
                self.assertEqual(self.sent_room(cwd, "--room", "."), self.room)

    def test_worktree_resolves_to_the_main_repo_basename(self):
        repo = self.repo(self.room)
        worktree = os.path.join(self.tmp, "elsewhere")
        self.git(repo, "worktree", "add", "-q", worktree, "-b", "side")
        res = self.at(worktree, "send", "--room", ".", "--from", "a", "--json", "from the worktree")
        self.assertEqual(json.loads(res.stdout)["room"], self.room)
        self.assertEqual([m["body"] for m in self.stored()], ["from the worktree"])

    def test_bare_repository_resolves_to_its_name_without_the_git_suffix(self):
        bare = self.directory(f"{self.room}.git")
        self.git(bare, "init", "-q", "--bare")
        self.assertEqual(self.sent_room(bare, "--room", "."), self.room)

    def test_outside_git_falls_back_to_the_sanitised_directory_name(self):
        cwd = self.directory(f"{self.room} my app!")
        self.assertEqual(self.sent_room(cwd, "--room", "."), f"{self.room}-my-app")

    def test_names_that_sanitise_to_nothing_or_to_dot_dot_become_main(self):
        for name in ("~~~", ".. "):
            with self.subTest(name=name):
                self.assertEqual(store.sanitize_room(name), "main")

    def test_swarm_room_environment_dot_resolves_too(self):
        repo = self.repo(self.room)
        self.assertEqual(self.sent_room(repo, SWARM_ROOM="."), self.room)

    def test_dot_counts_as_an_explicit_room_for_guarded_commands(self):
        repo = self.repo(self.room)
        for name, argv in (("before", ("--room", ".", "archive")), ("after", ("archive", "--room", "."))):
            with self.subTest(name):
                self.sent_room(repo, "--room", ".")
                self.r.delete(store.k_presence(self.room, "a"))
                res = self.at(repo, *argv, "--json")
                self.assertEqual(json.loads(res.stdout)["room"], self.room)
                archive_id = json.loads(res.stdout)["archive_id"]
                self.at(repo, "unarchive", archive_id)
                self.at(repo, "clear", "--room", ".", "--force")

    def test_invalid_and_traversing_room_names_exit_2_and_write_nothing(self):
        for value in ("..", "foo/bar", "a b", "a:b", ""):
            for argv in (("--room", value, "send", "--from", "a", "x"), ("send", "--room", value, "--from", "a", "x")):
                with self.subTest(value=value, argv=argv):
                    self.at(self.tmp, *argv, rc=2)
                    self.assertFalse(value and self.r.exists(store.k_stream(value)))
        self.assertEqual(list(self.r.scan_iter(match=f"*{self.room}*")), [])


class WorkerRoomE2E(CliE2E):
    def sentinel(self, agent):
        return os.path.join(self.env["SWARM_STATE_DIR"], self.room, f"stop-{agent}")

    def retired(self, proc, agent):
        self.assertEqual(proc.wait(timeout=DEADLINE), 0)
        self.assertEqual(self.member(agent)["state"], "retired")

    def waiting(self, agent):
        self.wait_for(lambda: self.member(agent).get("state") == "waiting")


@unittest.skipUnless(redis_up(), "local Redis not reachable")
class DoneE2E(WorkerRoomE2E):
    def lead(self):
        self.join("coordinator", "--role", "coordinator")

    def test_default_summary_goes_to_all_as_kind_done(self):
        self.lead()
        res = self.run_swarm("done")
        [(msg_id, fields)] = self.r.xrange(store.k_stream(self.room))
        self.assertEqual(fields, store.make_fields("coordinator", "all", "Swarm finished.", kind="done"))
        self.assertEqual(res.stdout, f"sent {msg_id} to room={self.room} to=all (done)\n")
        self.assertEqual(res.stderr, "")

    def test_terminal_stdin_uses_the_default_without_reading(self):
        self.lead()
        try:
            leader, follower = pty.openpty()
        except OSError as exc:
            self.skipTest(f"no pty available: {exc}")
        try:
            res = subprocess.run(
                [sys.executable, "-m", "swarm", "--url", URL, "--room", self.room, "done"],
                stdin=follower, capture_output=True, text=True, env=self.env, timeout=DEADLINE,
            )
        finally:
            os.close(leader)
            os.close(follower)
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual([m["body"] for m in self.stored()], ["Swarm finished."])

    def test_body_comes_from_argument_dash_or_pipe(self):
        self.lead()
        self.run_swarm("done", "shipped it", stdin="ignored")
        self.run_swarm("done", "-", stdin="\ndash body\n")
        self.run_swarm("done", stdin="piped body\n")
        self.assertEqual([m["body"] for m in self.stored()], ["shipped it", "dash body", "piped body"])

    def test_explicit_empty_body_exits_2_and_writes_nothing(self):
        for name, args, stdin in (("empty argument", ("",), ""), ("dash", ("-",), "  \n")):
            with self.subTest(name):
                res = self.run_swarm("done", *args, stdin=stdin, rc=2)
                self.assertIn("empty body", res.stderr)
                self.assertEqual(self.stored(), [])

    def test_json_and_flags_work_before_and_after_the_subcommand(self):
        self.lead()
        flags = ("--url", URL, "--room", self.room, "--json")
        for argv in ((*flags, "done", "before"), ("done", "after", *flags)):
            with self.subTest(argv=argv):
                res = self.cli(*argv, SWARM_REDIS_URL=DEAD_URL)
                self.assertEqual(res.returncode, 0, res.stderr)
                last_id = self.r.xrevrange(store.k_stream(self.room), count=1)[0][0]
                self.assertEqual(json.loads(res.stdout), {"id": last_id, "room": self.room, "kind": "done"})

    def test_warns_when_the_sender_cannot_end_the_swarm(self):
        self.join("coord", "--role", "coordinator")
        res = self.run_swarm("done")
        self.assertIn("'coordinator' does not have the coordinator role", res.stderr)
        self.assertIn("--from", res.stderr)
        self.assertEqual(len(self.stored()), 1)

    def test_retires_a_real_worker(self):
        worker = self.start_worker()
        self.waiting("w1")
        self.run_swarm("done", "--from", "coord", "all merged")
        self.retired(worker, "w1")
        self.assertEqual(self.member("w1")["doing"], "all merged")


@unittest.skipUnless(redis_up(), "local Redis not reachable")
class StopE2E(WorkerRoomE2E):
    def test_posts_shutdown_to_one_agent_and_writes_its_sentinel(self):
        self.join("w1")
        res = self.run_swarm("stop", "w1", "scope changed")
        [(msg_id, fields)] = self.r.xrange(store.k_stream(self.room))
        self.assertEqual(fields, store.make_fields("coordinator", "w1", store.SHUTDOWN))
        self.assertEqual(res.stdout, f"stopped w1 in room={self.room} (sent {msg_id})\n")
        self.assertEqual(res.stderr, "")
        with open(self.sentinel("w1")) as f:
            self.assertEqual(f.read(), "scope changed\n")
        self.assertEqual(os.listdir(os.path.dirname(self.sentinel("w1"))), ["stop-w1"])

    def test_json_from_and_flags_after_the_subcommand(self):
        self.join("w1")
        res = self.cli("stop", "w1", "--from", "lead", "--json", "--url", URL, "--room", self.room)
        self.assertEqual(res.returncode, 0, res.stderr)
        [(msg_id, fields)] = self.r.xrange(store.k_stream(self.room))
        self.assertEqual(json.loads(res.stdout), {"stopped": "w1", "room": self.room, "id": msg_id})
        self.assertEqual(fields["from"], "lead")

    def test_retires_only_the_target_worker_and_leaves_the_room_running(self):
        first = self.start_worker("w1")
        second = self.start_worker("w2")
        self.waiting("w1")
        self.waiting("w2")
        self.run_swarm("stop", "w1")
        self.retired(first, "w1")
        self.run_swarm("send", "--from", "coord", "--to", "w2", "still there?")
        self.wait_for(lambda: any(p["from"] == "w2" for p in self.posts()))
        self.assertIsNone(second.poll())
        self.assertFalse(os.path.exists(self.sentinel("w2")))
        self.assertFalse(os.path.exists(self.sentinel("all")))
        self.assertNotIn("done", [p["kind"] for p in self.posts()])

    def test_all_is_refused_because_it_would_retire_every_worker(self):
        res = self.run_swarm("stop", "all", rc=2)
        self.assertIn("swarm done", res.stderr)
        self.assertEqual(self.stored(), [])
        self.assertFalse(os.path.exists(self.sentinel("all")))

    def test_malformed_agent_ids_exit_2(self):
        for agent in ("@api", "a/b", "w1,w2"):
            with self.subTest(agent):
                self.run_swarm("stop", agent, rc=2)
        self.assertEqual(self.stored(), [])

    def test_unknown_agent_warns_and_still_stops(self):
        res = self.run_swarm("stop", "ghost")
        self.assertIn(f"warning: no agent 'ghost' in room={self.room}", res.stderr)
        self.assertTrue(os.path.exists(self.sentinel("ghost")))

    def test_unwritable_state_dir_exits_1_after_the_shutdown_was_sent(self):
        self.join("w1")
        blocker = os.path.join(self.tmp, "blocker")
        open(blocker, "w").close()
        res = self.cli("--url", URL, "--room", self.room, "stop", "w1", SWARM_STATE_DIR=blocker)
        self.assertEqual(res.returncode, 1, res.stderr)
        self.assertIn("shutdown was sent", res.stderr)
        self.assertNotIn("Traceback", res.stderr)
        self.assertEqual([m["body"] for m in self.stored()], [store.SHUTDOWN])


@unittest.skipUnless(redis_up(), "local Redis not reachable")
class LogsE2E(CliE2E):
    def write_log(self, data, agent="w1", mode="wb"):
        directory = os.path.join(self.env["SWARM_STATE_DIR"], self.room)
        os.makedirs(directory, exist_ok=True)
        with open(os.path.join(directory, f"{agent}.log"), mode) as f:
            f.write(data)

    def logs(self, *args, rc=0):
        res = self.cli("--url", DEAD_URL, "--room", self.room, "logs", *args)
        self.assertEqual(res.returncode, rc, res.stdout + res.stderr)
        return res

    def numbered(self, first, last):
        return "".join(f"line {i}\n" for i in range(first, last + 1))

    def test_prints_the_last_50_lines_by_default_without_redis(self):
        self.write_log(self.numbered(1, 60).encode())
        self.assertEqual(self.logs("w1").stdout, self.numbered(11, 60))

    def test_n_limits_the_tail(self):
        self.write_log(self.numbered(1, 5).encode())
        self.assertEqual(self.logs("w1", "-n", "2").stdout, self.numbered(4, 5))
        self.assertEqual(self.logs("w1", "-n", "0").stdout, "")
        self.assertEqual(self.logs("w1", "-n", "99").stdout, self.numbered(1, 5))
        self.logs("w1", "-n", "-1", rc=2)

    def test_untrusted_bytes_are_made_safe_to_print(self):
        self.write_log(b"\x1b[31mred\x07 \xff tail")
        self.assertEqual(self.logs("w1").stdout, "\\x1b[31mred\\x07 � tail")

    def test_missing_log_exits_1_and_names_the_path(self):
        for flags in ((), ("-f",)):
            with self.subTest(flags=flags):
                res = self.logs("nobody", *flags, rc=1)
                self.assertIn(os.path.join(self.env["SWARM_STATE_DIR"], self.room, "nobody.log"), res.stderr)
                self.assertEqual(res.stdout, "")
        self.assertFalse(os.path.exists(self.env["SWARM_STATE_DIR"]))

    def test_unreadable_log_exits_1_without_a_traceback(self):
        os.makedirs(os.path.join(self.env["SWARM_STATE_DIR"], self.room, "w1.log"))
        res = self.logs("w1", rc=1)
        self.assertIn("cannot read", res.stderr)
        self.assertNotIn("Traceback", res.stderr)

    def test_follow_streams_appended_lines_until_interrupted(self):
        self.write_log(self.numbered(1, 3).encode())
        proc = subprocess.Popen(
            [sys.executable, "-m", "swarm", "--room", self.room, "logs", "w1", "-n", "1", "--follow"],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, encoding="utf-8",
            env=self.env, preexec_fn=lambda: signal.signal(signal.SIGINT, signal.SIG_DFL),
        )
        self.procs.append(proc)
        stream = LineStream(proc)
        self.streams.append(stream)
        self.assertEqual(stream.take(1), ["line 3"])
        self.write_log(b"line 4\nline 5\n", mode="ab")
        self.assertEqual(stream.take(2), ["line 4", "line 5"])
        check = "café\n".encode()
        self.write_log(check[:4], mode="ab")
        self.write_log(check[4:], mode="ab")
        self.assertEqual(stream.take(1), ["café"])
        proc.send_signal(signal.SIGINT)
        self.assertEqual(proc.wait(timeout=DEADLINE), 0)
        self.assertEqual(proc.stderr.read(), "")
        proc.stderr.close()


@unittest.skipUnless(redis_up(), "local Redis not reachable")
class InitE2E(CliE2E):
    def setUp(self):
        super().setUp()
        self.home = os.path.join(self.tmp, "home")
        self.bin = os.path.join(self.tmp, "bin")
        os.makedirs(self.home)
        os.makedirs(self.bin)
        self.config = os.path.join(self.tmp, "nested", "dir", "config.yaml")
        self.env.update(HOME=self.home, SWARM_CONFIG=self.config, PATH=self.bin)
        self.env.pop("SWARM_HOME", None)

    def install(self, *names):
        for name in names:
            path = os.path.join(self.bin, name)
            with open(path, "w") as f:
                f.write("#!/bin/sh\n")
            os.chmod(path, 0o755)

    def init(self, *args, rc=0, url=URL, **env):
        res = self.cli("--url", url, "init", *args, **env)
        self.assertEqual(res.returncode, rc, res.stdout + res.stderr)
        return res

    def report(self, *args, **kw):
        return json.loads(self.init("--json", *args, **kw).stdout)

    def read(self, path):
        with open(path) as f:
            return f.read()

    def write(self, path, text):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write(text)

    def test_writes_only_detected_seats_plus_cmd(self):
        self.install("claude", "codex")
        res = self.init()
        cfg = swarm_config.load(self.config)
        self.assertEqual(list(cfg["seats"]), ["claude", "codex", "cmd"])
        self.assertEqual({seat for model in cfg["models"] for seat in model["seats"]}, {"claude", "codex"})
        self.assertEqual(cfg["claude_flags"], swarm_config.load(os.path.join(self.tmp, "none"))["claude_flags"])
        self.assertIn("\n# ", self.read(self.config))
        self.assertIn(self.config, res.stdout)
        self.assertIn("claude, codex, cmd", res.stdout)
        self.assertRegex(res.stdout, r"redis: +ok")

    def test_every_detected_writer_can_be_picked_for_both_roles(self):
        cases = {("claude",): ("claude", "claude"), ("codex",): ("codex", "codex"),
                 ("claude", "codex", "agy"): ("codex", "agy")}
        for binaries, (worker, verifier) in cases.items():
            with self.subTest(binaries=binaries):
                shutil.rmtree(self.bin)
                os.makedirs(self.bin)
                self.install(*binaries)
                self.init("--force")
                cfg = swarm_config.load(self.config)
                self.assertEqual(swarm_config.choose(cfg, "worker", {})["harness"], worker)
                self.assertEqual(swarm_config.choose(cfg, "verifier", {})["harness"], verifier)

    def test_a_seat_without_a_starter_model_still_yields_a_loadable_config(self):
        self.install("omp")
        self.init()
        cfg = swarm_config.load(self.config)
        self.assertEqual(list(cfg["seats"]), ["omp", "cmd"])
        self.assertEqual(cfg["models"], [])

    def test_nothing_detected_writes_every_default_seat(self):
        report = self.report()
        self.assertEqual(report["detected"], [])
        self.assertEqual(list(swarm_config.load(self.config)["seats"]), list(swarm_config.DEFAULT_SEATS))

    def test_json_report(self):
        self.install("agy")
        self.assertEqual(
            self.report(),
            {"config": self.config, "detected": ["agy"], "seats": ["agy", "cmd"],
             "redis": {"url": URL, "ok": True}, "skills": [], "notes": []},
        )

    def test_unreachable_redis_is_reported_and_the_config_still_written(self):
        report = self.report(url=DEAD_URL)
        self.assertFalse(report["redis"]["ok"])
        self.assertTrue(report["redis"]["error"])
        self.assertTrue(os.path.exists(self.config))

    def test_refuses_to_overwrite_without_force(self):
        self.write(self.config, "mine: true\n")
        self.install("claude")
        res = self.init("--link-skills", rc=1)
        self.assertIn("--force", res.stderr)
        self.assertIn(self.config, res.stderr)
        self.assertEqual(self.read(self.config), "mine: true\n")
        self.assertEqual(os.listdir(self.home), [])
        self.init("--force")
        self.assertIn("claude", swarm_config.load(self.config)["seats"])

    def test_unwritable_config_path_exits_1_without_a_traceback(self):
        os.makedirs(self.config)
        res = self.init("--force", rc=1)
        self.assertIn(self.config, res.stderr)
        self.assertNotIn("Traceback", res.stderr)

    def test_default_path_is_the_primary_config_under_home(self):
        self.env.pop("SWARM_CONFIG")
        primary = os.path.join(self.home, ".config", "swarm", "config.yaml")
        self.assertEqual(self.report()["config"], primary)
        self.assertTrue(os.path.exists(primary))

    def test_force_never_overwrites_the_legacy_messagebus_config(self):
        self.env.pop("SWARM_CONFIG")
        legacy = os.path.join(self.home, ".config", "messagebus", "swarm.yaml")
        primary = os.path.join(self.home, ".config", "swarm", "config.yaml")
        self.write(legacy, "legacy: true\n")
        res = self.init(rc=1)
        self.assertIn(legacy, res.stderr)
        self.assertFalse(os.path.exists(primary))
        report = self.report("--force")
        self.assertEqual(report["config"], primary)
        self.assertIn(legacy, " ".join(report["notes"]))
        self.assertEqual(self.read(legacy), "legacy: true\n")

    def skill_link(self, harness, name, home=None):
        return os.path.join(home or self.home, harness, "skills", name)

    def assert_linked(self, harness, home=None):
        for name, source in (("swarm", "swarm"), ("swarm-rooms", "rooms")):
            link = self.skill_link(harness, name, home)
            self.assertTrue(os.path.islink(link), link)
            self.assertEqual(os.path.realpath(link), os.path.realpath(os.path.join(ROOT, "skills", source)))

    def test_link_skills_defaults_to_claude_when_no_harness_home_exists(self):
        report = self.report("--link-skills")
        self.assert_linked(".claude")
        self.assertFalse(os.path.exists(os.path.join(self.home, ".gemini")))
        self.assertEqual([s["status"] for s in report["skills"]], ["linked", "linked"])

    def test_link_skills_covers_each_harness_present_and_is_repeatable(self):
        os.makedirs(os.path.join(self.home, ".gemini"))
        self.install("claude")
        self.report("--link-skills")
        report = self.report("--link-skills", "--force")
        self.assert_linked(".claude")
        self.assert_linked(".gemini")
        self.assertEqual({s["status"] for s in report["skills"]}, {"already linked"})

    def test_link_skills_never_deletes_a_real_directory(self):
        keep = os.path.join(self.skill_link(".claude", "swarm"), "SKILL.md")
        self.write(keep, "hand written\n")
        report = self.report("--link-skills", "--force")
        self.assertEqual(self.read(keep), "hand written\n")
        self.assertEqual([s["status"] for s in report["skills"]], ["skipped: not a symlink", "linked"])

    def test_link_skills_replaces_a_foreign_symlink_only_with_force(self):
        other = os.path.join(self.tmp, "other-skill")
        os.makedirs(other)
        link = self.skill_link(".claude", "swarm")
        os.makedirs(os.path.dirname(link))
        os.symlink(other, link)
        kept = self.report("--link-skills")
        self.assertEqual(os.readlink(link), other)
        self.assertEqual(kept["skills"][0]["status"], "skipped: links elsewhere (--force replaces it)")
        replaced = self.report("--link-skills", "--force")
        self.assertEqual(replaced["skills"][0]["status"], "replaced")
        self.assert_linked(".claude")
        self.assertTrue(os.path.isdir(other))

    def test_swarm_home_overrides_home_for_skill_links(self):
        other = os.path.join(self.tmp, "swarm-home")
        self.report("--link-skills", SWARM_HOME=other)
        self.assert_linked(".claude", home=other)
        self.assertEqual(os.listdir(self.home), [])

    def test_link_failure_is_reported_and_exits_1(self):
        self.write(os.path.join(self.home, ".claude"), "a file where a directory belongs")
        report = self.report("--link-skills", rc=1)
        self.assertTrue(all(s["status"].startswith("failed: ") for s in report["skills"]), report)

    def test_install_without_bundled_skills_prints_a_note(self):
        lib = os.path.join(self.tmp, "wheel", "lib")
        shutil.copytree(os.path.join(SRC, "swarm"), os.path.join(lib, "swarm"))
        report = self.report("--link-skills", PYTHONPATH=lib)
        self.assertEqual(report["skills"], [])
        self.assertIn("no bundled skills", " ".join(report["notes"]))
        self.assertEqual(os.listdir(self.home), [])


if __name__ == "__main__":
    unittest.main()
