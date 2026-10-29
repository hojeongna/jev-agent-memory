"""Stage 1 capture, stage 2 digest (+ same-run labeling), daily consolidation and schedule
generation — fakes only (no LLM, no Jev, no launchctl, no real crontab)."""
import json
import os
import plistlib
import sqlite3
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from helpers import DEFAULT_RULES, FakeJev, JevError, TempProject, keyword_labeler, run_cli

from jevmem import capture, consolidate, digest, hook, llm, schedule, status


class FakeLLM(llm.Backend):
    name = "fake"

    def __init__(self, reply=None):
        self.reply = reply
        self.prompts = []

    def complete(self, prompt):
        self.prompts.append(prompt)
        return self.reply(prompt) if callable(self.reply) else self.reply


def tg(text, mid, ts="2026-09-10T00:05:00.000Z", chat="111", user="someone"):
    return (f'<channel source="plugin:telegram:telegram" chat_id="{chat}" message_id="{mid}" '
            f'user="{user}" ts="{ts}">{text}</channel>')


def dc(text, mid, chat="222"):
    return (f'<channel source="plugin:discord:discord" chat_id="{chat}" message_id="{mid}" '
            f'user="someone" ts="2026-09-10T00:06:00Z">{text}</channel>')


def reply_payload(text, tool="mcp__plugin_telegram_telegram__reply", chat="111", resp="sent (id: 853)"):
    return {"tool_name": tool, "tool_input": {"chat_id": chat, "text": text}, "tool_response": resp}


class TestCapture(unittest.TestCase):
    def setUp(self):
        self.tp = TempProject(logs=False)
        self.cfg = self.tp.cfg()

    def tearDown(self):
        self.tp.cleanup()

    def test_inbound_telegram_and_discord_blocks_only(self):
        prompt = ("Some system text\n" + tg("Why did the backup fail?", "10") + "\n" + dc("hello from discord", "77")
                  + '\n<channel source="plugin:other:thing" chat_id="1" message_id="1">ignored</channel>'
                  + "\n<task-notification>internal</task-notification>")
        self.assertEqual(capture.inbound(self.cfg, {"prompt": prompt}), 2)
        recs = capture.tail(self.cfg)
        self.assertEqual([r["channel"] for r in recs], ["telegram", "discord"])
        r = recs[0]
        self.assertEqual((r["dir"], r["chat_id"], r["message_id"], r["user"], r["text"]),
                         ("in", "111", "10", "someone", "Why did the backup fail?"))
        self.assertEqual(datetime.fromisoformat(r["ts"]),
                         datetime.fromisoformat("2026-09-10T00:05:00+00:00"), "UTC ts kept as the same instant")

    def test_inbound_dedupes_by_message_id(self):
        p = {"prompt": tg("same message", "10")}
        capture.inbound(self.cfg, p)
        capture.inbound(self.cfg, p)
        capture.inbound(self.cfg, {"prompt": tg("same id, other chat", "10", chat="999")})
        self.assertEqual(len(capture.tail(self.cfg)), 2)

    def test_plain_claude_code_prompt_is_not_captured(self):
        self.assertEqual(capture.inbound(self.cfg, {"prompt": "refactor the parser please"}), 0)
        self.assertFalse(capture.log_path(self.cfg).exists())

    def test_outbound_logs_only_chat_reply_tools(self):
        self.assertEqual(capture.outbound(self.cfg, reply_payload("Fixed: absolute path now.")), 1)
        self.assertEqual(capture.outbound(self.cfg, reply_payload("hi", tool="mcp__plugin_discord_discord__reply",
                                                                  chat="222", resp={"id": 5})), 1)
        self.assertEqual(capture.outbound(self.cfg, {"tool_name": "Bash", "tool_input": {"command": "ls"}}), 0)
        self.assertEqual(capture.outbound(self.cfg, reply_payload("")), 0)
        recs = capture.tail(self.cfg)
        self.assertEqual([(r["dir"], r["channel"], r["chat_id"], r["message_id"], r["text"]) for r in recs],
                         [("out", "telegram", "111", "853", "Fixed: absolute path now."),
                          ("out", "discord", "222", "5", "hi")])
        self.assertEqual(recs[0]["user"], "Assistant")

    def test_log_out_and_cli_hooks_never_fail(self):
        r = run_cli("capture", "log-out", "Daily", "alert:", "disk", "80%", "--channel", "telegram",
                    cfg_path=self.tp.cfg_path)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(capture.tail(self.cfg)[-1]["text"], "Daily alert: disk 80%")
        for action, payload in (("inbound", "not json"), ("outbound", "[]"),
                                ("inbound", json.dumps({"prompt": tg("via cli", "5")}))):
            r = run_cli("capture", action, cfg_path=self.tp.cfg_path, stdin=payload)
            self.assertEqual((r.returncode, r.stdout, r.stderr), (0, "", ""))
        self.assertEqual(capture.tail(self.cfg)[-1]["text"], "via cli")
        out = run_cli("capture", "tail", cfg_path=self.tp.cfg_path).stdout
        self.assertIn("[telegram]", out)

    def test_recall_context_reads_capture_log(self):
        from jevmem import recall
        capture.inbound(self.cfg, {"prompt": tg("how did the deploy go", "1")})
        capture.outbound(self.cfg, reply_payload("staging is green"))
        capture.inbound(self.cfg, {"prompt": tg("and the status endpoint?", "2")})
        self.assertEqual(recall.prior_lines(self.cfg, "and the status endpoint?"),
                         ["User: how did the deploy go", "Assistant: staging is green"])


class _DigestCase(unittest.TestCase):
    def setUp(self):
        self.tp = TempProject(logs=False)
        self.cfg = self.tp.cfg({"digest": {"index_after": False}})
        self.out = []
        self.now = datetime(2026, 9, 10, 9, 34)

    def tearDown(self):
        self.tp.cleanup()

    def run_d(self, backend, cfg=None, **kw):
        return digest.run(cfg or self.cfg, backend=backend, now=self.now, out=self.out.append, **kw)

    def daily(self):
        return (self.tp.root / "memory" / "2026-09-10.md").read_text(encoding="utf-8")

    def user(self, text, mid):
        capture.inbound(self.cfg, {"prompt": tg(text, mid)})

    def bot(self, text):
        capture.outbound(self.cfg, reply_payload(text))


class TestDigest(_DigestCase):
    def test_summarizes_captured_chat_and_advances_offset(self):
        self.user("Why did the nightly backup fail?", "1")
        self.bot("The cron used a relative path; fixed it.")
        fake = FakeLLM('```json\n{"bullets": ["User asked why the backup failed.", "- Assistant fixed the relative path."]}\n```')
        res = self.run_d(fake)
        self.assertEqual(res["result"], "ok", "capture log is read from the start (no baseline)")
        self.assertIn("User: Why did the nightly backup fail?", fake.prompts[0])
        self.assertIn("Assistant: The cron used a relative path", fake.prompts[0])
        text = self.daily()
        self.assertTrue(text.startswith("# 2026-09-10 daily log\n"))
        self.assertIn("\n## 09:34 summary\n- User asked why the backup failed.\n- Assistant fixed the relative path.\n", text)
        fake2 = FakeLLM('{"bullets": ["should not appear"]}')
        self.assertEqual(self.run_d(fake2)["result"], "skip")
        self.assertEqual(fake2.prompts, [])
        self.assertEqual(self.daily(), text)

    def test_one_bullet_per_topic_and_legacy_formats(self):
        self.user("fix the backup job, and also book the restaurant for friday", "1")
        topics = ["Backup job: cron used a relative path; fixed to absolute.",
                  "Restaurant for Friday 7 pm booked for six people.",
                  "t3 topic three", "t4 topic four", "t5 topic five", "t6 over the limit"]
        fake = FakeLLM(json.dumps({"topics": topics}))
        self.run_d(fake)
        self.assertIn('{"topics": [', fake.prompts[0])
        self.assertIn("One item per distinct topic, at most 5 items", fake.prompts[0])
        self.assertIn("COMPLETE sentences", fake.prompts[0])
        self.assertIn("must not be written as done", fake.prompts[0])
        section = self.daily().split("## 09:34 summary\n", 1)[1]
        self.assertEqual(section.splitlines(), [f"- {t}" for t in topics[:5]], "one '- ' bullet per topic, max 5")
        # legacy {"summary": "..."} -> exactly one bullet; dict items tolerated
        cfg = self.cfg
        self.assertEqual(digest.summarize(cfg, FakeLLM('{"summary": "One legacy summary line."}'), "c"),
                         ["One legacy summary line."])
        self.assertEqual(digest.summarize(cfg, FakeLLM('{"topics": [{"topic": "x", "summary": "Deploy done."}, ""]}'), "c"),
                         ["Deploy done."])
        self.assertIsNone(digest.summarize(cfg, FakeLLM('{"topics": []}'), "c"))

    def test_skips_without_new_user_message(self):
        self.bot("Scheduled report: all green.")
        capture.log_out(self.cfg, "cron alert: disk 80%")
        fake = FakeLLM('{"bullets": ["x"]}')
        self.assertEqual(self.run_d(fake)["result"], "skip")
        self.assertEqual(fake.prompts, [])
        self.assertFalse((self.tp.root / "memory" / "2026-09-10.md").exists())

    def test_partial_last_line_waits_for_next_run(self):
        self.user("complete line message", "1")
        with open(capture.log_path(self.cfg), "a", encoding="utf-8") as f:
            f.write('{"dir": "in", "text": "half wri')
        fake = FakeLLM('{"bullets": ["one"]}')
        self.run_d(fake)
        self.assertNotIn("half", fake.prompts[0])
        with open(capture.log_path(self.cfg), "a", encoding="utf-8") as f:
            f.write('tten"}\n')
        fake2 = FakeLLM('{"bullets": ["two"]}')
        self.run_d(fake2)
        self.assertIn("half written", fake2.prompts[0])
        self.assertNotIn("complete line message", fake2.prompts[0])

    def test_backend_failure_writes_raw_lines(self):
        self.user("please remember the dentist on Friday", "1")
        res = self.run_d(FakeLLM("not json at all"))
        self.assertEqual(res["result"], "raw")
        self.assertIn("## 09:34 summary (raw)\n- [", self.daily())
        self.assertIn("please remember the dentist on Friday", self.daily())

    def test_dry_run_writes_nothing_and_keeps_offsets(self):
        self.user("dry run content here", "1")
        res = self.run_d(FakeLLM('{"bullets": ["b"]}'), dry_run=True)
        self.assertTrue(res["dry_run"])
        self.assertFalse((self.tp.root / "memory" / "2026-09-10.md").exists())
        self.assertEqual(self.run_d(FakeLLM('{"bullets": ["b"]}'))["result"], "ok")

    def test_same_run_labels_new_chunks_and_retries_after_failure(self):
        cfg = self.tp.cfg()  # index_after = true
        self.user("The nightly backup job failed because of a relative path in cron", "1")
        bad = FakeJev(error=JevError("Jev HTTP 500"))
        res = self.run_d(FakeLLM('{"bullets": ["Nightly backup failed: relative path in the cron entry."]}'),
                         cfg=cfg, client_factory=lambda: bad)
        self.assertEqual(res["result"], "ok")
        self.assertIn("relative path", self.daily(), "summary kept even though labeling failed")
        self.assertEqual(res["index"]["labeled"], 0)
        self.assertGreaterEqual(res["index"]["failed"], 1)
        good = FakeJev(labeler=keyword_labeler(DEFAULT_RULES))
        self.user("thanks, also book the restaurant for friday", "2")
        res = self.run_d(FakeLLM('{"bullets": ["Restaurant to be booked for Friday."]}'), cfg=cfg,
                         client_factory=lambda: good)
        self.assertEqual(res["index"]["labeled"], 2, "the failed chunk is retried + the new one labeled")
        con = sqlite3.connect(cfg["data"]["episodic_db"])
        self.assertEqual(con.execute("select count(*) from units where labeled_at is null").fetchone()[0], 0)
        con.close()
        self.assertEqual(status.read(cfg)["index"]["result"], "ok")

    def test_index_step_without_key_is_deferred_not_fatal(self):
        cfg = self.tp.cfg()
        self.user("remember that the standup moved to 10am", "1")
        res = self.run_d(FakeLLM('{"bullets": ["The daily standup moved to 10am starting next week."]}'), cfg=cfg)
        self.assertEqual(res["result"], "ok")
        self.assertIn("No Jev API key", res["index"].get("error", ""), res["index"])
        self.assertIn("standup moved to 10am", self.daily())


def urec(text, ts="2026-09-10T09:00:00", **kw):
    return {"type": "user", "timestamp": ts, "message": {"role": "user", "content": text}, **kw}


def arec(blocks, ts="2026-09-10T09:01:00", **kw):
    return {"type": "assistant", "timestamp": ts, "message": {"role": "assistant", "content": blocks}, **kw}


def append_jsonl(path: Path, recs):
    with open(path, "a", encoding="utf-8") as f:
        for r in recs:
            f.write(json.dumps(r) + "\n")


class TestDigestClaudeTranscriptFallback(_DigestCase):
    def setUp(self):
        super().setUp()
        self._home = tempfile.TemporaryDirectory()
        self.cfg = self.tp.cfg({"digest": {"index_after": False, "source": "claude_transcript",
                                           "claude_projects_dir": str(Path(self._home.name) / "projects")}})
        self.sdir = digest.transcript_dir(self.cfg)
        self.sdir.mkdir(parents=True)
        self.session = self.sdir / "s1.jsonl"
        append_jsonl(self.session, [urec("old history that must not be summarized")])

    def tearDown(self):
        super().tearDown()
        self._home.cleanup()

    def test_slug(self):
        self.assertEqual(digest.project_slug("/nonexistent-dir/a.b/my_agent"), "-nonexistent-dir-a-b-my-agent")

    def test_baseline_then_noise_filtered(self):
        self.assertEqual(self.run_d(FakeLLM())["result"], "baseline")
        append_jsonl(self.session, [
            urec("Why did the nightly backup fail?"),
            arec([{"type": "thinking", "thinking": "hmm"}, {"type": "tool_use", "name": "Bash", "input": {}}]),
            urec([{"type": "tool_result", "content": "noise"}]),
            arec([{"type": "text", "text": "The cron used a relative path; fixed it."}]),
            urec("subagent chatter", isSidechain=True),
            urec("<command-name>/clear</command-name>"),
        ])
        append_jsonl(self.sdir / "s2.jsonl", [urec("message in a brand new session")])
        fake = FakeLLM('{"bullets": ["ok"]}')
        self.assertEqual(self.run_d(fake)["result"], "ok")
        prompt = fake.prompts[0]
        self.assertIn("brand new session", prompt)
        self.assertIn("Assistant: The cron used a relative path", prompt)
        for noise in ("old history", "noise", "hmm", "subagent chatter", "/clear"):
            self.assertNotIn(noise, prompt)


LOG = """# 2026-09-02 daily log

## 10:15 summary
- User said: "keep status updates to short bullet summaries, no long paragraphs".
- Drafted the export API spec.

## 21:00 summary
- User felt exhausted after the long day.
"""


class TestConsolidate(unittest.TestCase):
    def setUp(self):
        self.tp = TempProject(logs=False)
        (self.tp.root / "memory" / "2026-09-02.md").write_text(LOG, encoding="utf-8")
        self.cfg = self.tp.cfg()
        self.db = Path(self.cfg["data"]["profile_db"])
        self.out = []

    def tearDown(self):
        self.tp.cleanup()

    def run_c(self, reply, **kw):
        return consolidate.run(self.cfg, day="2026-09-02", backend=FakeLLM(reply), out=self.out.append, **kw)

    def traits(self):
        if not self.db.exists():
            return []
        con = sqlite3.connect(self.db)
        con.row_factory = sqlite3.Row
        rows = con.execute("SELECT * FROM traits ORDER BY id").fetchall()
        con.close()
        return rows

    GOOD = {"kind": "preference", "statement": "Prefers short bullet summaries for status updates",
            "quote": "keep status updates to short bullet summaries", "relation": "new"}

    def test_writes_verified_trait(self):
        res = self.run_c(json.dumps({"traits": [self.GOOD]}))
        self.assertEqual(res["decisions"][0]["status"], "written")
        rows = self.traits()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["statement"], self.GOOD["statement"])
        con = sqlite3.connect(self.db)
        src = con.execute("SELECT source_path, source_date FROM evidence").fetchone()
        con.close()
        self.assertEqual(src, ("memory/2026-09-02.md", "2026-09-02"))

    def test_rejects_non_verbatim_quote_emotion_and_bad_kind(self):
        cands = [dict(self.GOOD, quote="prefers bullets"),                          # not in log
                 {"kind": "pattern", "statement": "Gets exhausted after long days",  # emotion
                  "quote": "User felt exhausted after the long day"},
                 {"kind": "emotion", "statement": "Tired", "quote": "felt exhausted"}]
        res = self.run_c(json.dumps({"traits": cands}))
        reasons = [d["reason"] for d in res["decisions"]]
        self.assertIn("quote not found verbatim in the log", reasons[0])
        self.assertIn("emotion", reasons[1])
        self.assertIn("not allowed", reasons[2])
        self.assertEqual(self.traits(), [])

    def test_dry_run_writes_nothing_and_never_calls_the_llm(self):
        fake = FakeLLM(json.dumps({"traits": [self.GOOD]}))
        res = consolidate.run(self.cfg, day="2026-09-02", backend=fake, out=self.out.append, dry_run=True)
        self.assertEqual(fake.prompts, [], "dry-run never calls the LLM")
        self.assertEqual((res["result"], res["llm_called"]), ("dry-run", False))
        self.assertEqual((res["log_chars"], res["sent_chars"], res["active_traits"]), (len(LOG), len(LOG), 0))
        self.assertTrue(any("LLM not called" in str(o) for o in self.out))
        self.assertFalse(self.db.exists())

    def test_similar_new_becomes_merge_and_invalid_target_becomes_new(self):
        self.run_c(json.dumps({"traits": [self.GOOD]}))
        dup = dict(self.GOOD, statement="Prefers short bullet summaries for status updates",
                   quote="no long paragraphs", relation="new")
        res = self.run_c(json.dumps({"traits": [dup]}), backfill=True)
        self.assertEqual(res["decisions"][0]["action"], "merge")
        rows = self.traits()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["evidence_count"], 2)
        bad = dict(self.GOOD, kind="goal", statement="Wants the export API spec finished",
                   quote="Drafted the export API spec", relation="supersede", target_id=999)
        res = self.run_c(json.dumps({"traits": [bad]}), backfill=True)
        self.assertEqual(res["decisions"][0]["action"], "new")

    def test_supersede_existing(self):
        self.run_c(json.dumps({"traits": [self.GOOD]}))
        tid = self.traits()[0]["id"]
        sup = {"kind": "preference", "statement": "Wants status updates as three bullets max",
               "quote": "no long paragraphs", "relation": "supersede", "target_id": tid}
        self.run_c(json.dumps({"traits": [sup]}), backfill=True)
        rows = self.traits()
        self.assertEqual([r["status"] for r in rows], ["superseded", "active"])

    def test_budget_caps_writes(self):
        cands = [dict(self.GOOD, statement=f"Distinct durable trait number {i} about planning {i}",
                      kind="goal", quote="Drafted the export API spec") for i in range(3)]
        # pre-fill two evidence rows for that source date
        run_cli("profile", "new", "--kind", "skill", "--statement", "Writes API specs",
                "--source", "memory/2026-09-02.md", "--quote", "Drafted the export API spec", "--commit",
                cfg_path=self.tp.cfg_path)
        run_cli("profile", "new", "--kind", "skill", "--statement", "Writes clear specs",
                "--source", "memory/2026-09-02.md", "--quote", "Drafted the export", "--commit",
                cfg_path=self.tp.cfg_path)
        res = self.run_c(json.dumps({"traits": cands}), backfill=True)
        statuses = [d.get("status") or d["action"] for d in res["decisions"]]
        self.assertEqual(statuses.count("written"), 1)
        self.assertEqual(statuses.count("skip"), 2)

    def test_parse_failure_and_missing_log(self):
        self.assertEqual(self.run_c("I think the user likes bullets")["result"], "bad-llm-output")
        res = consolidate.run(self.cfg, day="2026-01-01", backend=FakeLLM("{}"), out=self.out.append)
        self.assertEqual(res["result"], "no-log")

    def test_prompt_contains_existing_traits_and_log(self):
        self.run_c(json.dumps({"traits": [self.GOOD]}))
        fake = FakeLLM('{"traits": []}')
        consolidate.run(self.cfg, day="2026-09-02", backend=fake, out=self.out.append)
        self.assertIn("#1 [preference] Prefers short bullet summaries", fake.prompts[0])
        self.assertIn('<log date="2026-09-02">', fake.prompts[0])


class TestSchedule(unittest.TestCase):
    def setUp(self):
        self.tp = TempProject(logs=False)
        self.cfg = self.tp.cfg()
        self._d = tempfile.TemporaryDirectory()
        self.agents = Path(self._d.name)

    def tearDown(self):
        self.tp.cleanup()
        self._d.cleanup()

    def test_launchd_plists(self):
        out = []
        res = schedule.install(self.cfg, self.tp.cfg_path, platform="macos", agents_dir=self.agents,
                               out=out.append)
        self.assertFalse(res["written"])
        self.assertEqual(list(self.agents.iterdir()), [], "print only without --write")
        schedule.install(self.cfg, self.tp.cfg_path, platform="macos", agents_dir=self.agents, write=True,
                         load=False, python="/usr/bin/python3", out=out.append)
        plists = {p.name.rsplit(".", 2)[-2]: plistlib.loads(p.read_bytes()) for p in self.agents.glob("*.plist")}
        self.assertEqual(set(plists), {"digest", "consolidate"}, "no separate index job")
        self.assertEqual(plists["digest"]["StartCalendarInterval"], [{"Minute": 4}, {"Minute": 34}])
        self.assertEqual(plists["consolidate"]["StartCalendarInterval"], [{"Hour": 4, "Minute": 0}])
        self.assertEqual(plists["consolidate"]["ProgramArguments"][-2:], ["profile", "consolidate"])
        self.assertEqual(plists["digest"]["ProgramArguments"][:5],
                         ["/usr/bin/python3", "-m", "jevmem", "--config", str(self.tp.cfg_path.resolve())])
        self.assertTrue(all(schedule.installed(self.cfg, self.tp.cfg_path, "macos", self.agents).values()))
        schedule.uninstall(self.cfg, self.tp.cfg_path, platform="macos", agents_dir=self.agents, write=True,
                           out=out.append)
        self.assertEqual(list(self.agents.glob("*.plist")), [])

    def test_crontab_block_merge_is_idempotent_and_preserves_other_lines(self):
        store = {"text": "MAILTO=\"\"\n0 * * * * /usr/bin/true\n"}
        io = (lambda: store["text"], lambda t: store.update(text=t))
        for _ in range(2):
            schedule.install(self.cfg, self.tp.cfg_path, platform="linux", write=True, crontab_io=io,
                             python="/usr/bin/python3", out=lambda *_: None)
        text = store["text"]
        self.assertTrue(text.startswith('MAILTO=""\n0 * * * * /usr/bin/true\n'))
        self.assertEqual(text.count("# >>> jevmem"), 1)
        self.assertIn("\n4,34 * * * * cd ", text)
        self.assertIn(" digest >> ", text)
        self.assertNotIn(" index >> ", text)
        self.assertIn("\n0 4 * * * cd ", text)
        self.assertIn("profile consolidate", text)
        self.assertTrue(all(schedule.installed(self.cfg, self.tp.cfg_path, "linux", crontab_text=text).values()))
        schedule.uninstall(self.cfg, self.tp.cfg_path, platform="linux", write=True, crontab_io=io,
                           out=lambda *_: None)
        self.assertEqual(store["text"], 'MAILTO=""\n0 * * * * /usr/bin/true\n')

    def test_custom_times(self):
        cfg = self.tp.cfg({"schedule": {"consolidate_time": "03:30", "digest_minutes": [0, 30]}})
        block = schedule.cron_block(cfg, self.tp.cfg_path, "/usr/bin/python3")
        self.assertIn("\n30 3 * * * ", block)
        self.assertIn("\n0,30 * * * * ", block)


class TestClaudeBackendIsolation(unittest.TestCase):
    """The child `claude -p` must not load plugins (a channel plugin in the child steals the
    main session's message polling), must not use --bare (OAuth), and runs hook-free."""

    def run_with_fake_subprocess(self, fn):
        import subprocess as sp
        from unittest import mock
        seen = {}

        def fake_run(argv, **kw):
            i = argv.index("--mcp-config")
            seen.update(argv=list(argv), cwd=kw.get("cwd"), env=kw.get("env"), stdin=kw.get("stdin"),
                        input=kw.get("input"),
                        mcp=json.loads(Path(argv[i + 1]).read_text()),
                        cwd_files=sorted(os.listdir(kw["cwd"])))
            return sp.CompletedProcess(argv, 0, stdout='{"topics": ["ok"]}', stderr="")

        with mock.patch.object(llm, "run_child", side_effect=fake_run), \
                mock.patch.object(llm.shutil, "which", return_value="/fake/bin/claude"):
            result = fn()
        return seen, result

    def test_complete_argv(self):
        b = llm.ClaudeBackend(model="haiku")
        seen, out = self.run_with_fake_subprocess(lambda: b.complete("PROMPT TEXT"))
        self.assertEqual(out, '{"topics": ["ok"]}')
        argv = seen["argv"]
        self.assertEqual(argv[:4], ["/fake/bin/claude", "-p", "--model", "haiku"])
        self.assertNotIn("PROMPT TEXT", argv, "the prompt goes to stdin, never into argv")
        self.assertEqual(seen["input"], "PROMPT TEXT")
        self.assertEqual(argv[4], "--disallowedTools")
        self.assertTrue(argv[5].startswith("Bash,Edit,Write,NotebookEdit,WebFetch,WebSearch,Read,Glob,Grep"),
                        "no `--tools` support known: read/search/agent tools are denied too, as ONE argument")
        self.assertEqual(argv[6:8], ["--strict-mcp-config", "--mcp-config"])
        self.assertEqual(len(argv), 9, "nothing after the variadic --mcp-config value")
        self.assertNotIn("--bare", argv)
        self.assertEqual(seen["mcp"], {"mcpServers": {}})
        self.assertEqual(Path(argv[8]).parent, Path(seen["cwd"]))
        self.assertEqual(seen["cwd_files"], ["empty.json"], "empty temp cwd: no CLAUDE.md, no project settings")
        self.assertEqual(seen["env"][llm.DISABLE_ENV], "1")
        self.assertFalse(Path(seen["cwd"]).exists(), "temp dir cleaned up")

    def test_disallowed_tools_configurable_and_can_be_turned_off(self):
        tp = TempProject(logs=False)
        try:
            from jevmem import labeler
            b = llm.make_backend(tp.cfg({"llm": {"claude_disallowed_tools": "Bash, WebFetch"}}), "claude")
            seen, _ = self.run_with_fake_subprocess(lambda: b.complete("P"))
            self.assertEqual(seen["argv"][4], "--disallowedTools")
            self.assertTrue(seen["argv"][5].startswith("Bash,WebFetch,Read,"), seen["argv"])
            off = labeler.default_backend(tp.cfg({"llm": {"claude_disallowed_tools": ""}}))
            seen, _ = self.run_with_fake_subprocess(lambda: off.complete("P"))
            self.assertNotIn("--disallowedTools", seen["argv"])
            self.assertEqual(seen["argv"][4:6], ["--strict-mcp-config", "--mcp-config"])
            dflt = labeler.default_backend(tp.cfg())
            seen, _ = self.run_with_fake_subprocess(lambda: dflt.complete("P"))
            self.assertIn("--disallowedTools", seen["argv"], "labeling uses the same restriction")
        finally:
            tp.cleanup()

    def test_doctor_check_is_isolated_too(self):
        b = llm.ClaudeBackend()
        seen, (ok, _) = self.run_with_fake_subprocess(b.check)
        self.assertTrue(ok)
        self.assertEqual(seen["argv"][1:], ["--version", "--strict-mcp-config", "--mcp-config", seen["argv"][-1]])
        self.assertEqual(seen["env"][llm.DISABLE_ENV], "1")

    def test_digest_and_consolidate_go_through_the_isolated_backend(self):
        tp = TempProject()
        try:
            cfg = tp.cfg({"llm": {"backend": "claude"}, "digest": {"index_after": False}})
            self.assertIsInstance(llm.get_backend(cfg), llm.ClaudeBackend)
            capture.inbound(cfg, {"prompt": tg("summarize me please, it is about the backup", "1")})
            seen, res = self.run_with_fake_subprocess(
                lambda: digest.run(cfg, now=datetime(2026, 9, 10, 9, 34), out=lambda *_: None))
            self.assertEqual(res["result"], "ok")
            self.assertIn("--strict-mcp-config", seen["argv"])
            seen, _ = self.run_with_fake_subprocess(
                lambda: consolidate.run(cfg, day="2026-09-02", out=lambda *_: None))
            self.assertIn("--strict-mcp-config", seen["argv"])
        finally:
            tp.cleanup()

    def test_capture_hooks_are_noops_inside_the_child(self):
        tp = TempProject(logs=False)
        try:
            r = run_cli("capture", "inbound", cfg_path=tp.cfg_path, stdin=json.dumps({"prompt": tg("x" * 20, "1")}),
                        env_extra={llm.DISABLE_ENV: "1"})
            self.assertEqual(r.returncode, 0)
            self.assertFalse(capture.log_path(tp.cfg()).exists())
        finally:
            tp.cleanup()


class TestMisc(unittest.TestCase):
    def test_parse_json_lenient(self):
        self.assertEqual(llm.parse_json('Sure! {"bullets": ["a"]} hope that helps'), {"bullets": ["a"]})
        self.assertIsNone(llm.parse_json("nope"))

    def test_hook_disabled_inside_own_subprocesses(self):
        tp = TempProject()
        try:
            os.environ[llm.DISABLE_ENV] = "1"
            self.assertEqual(hook.run(tp.cfg(), {"prompt": "nightly backup relative path"}), "")
        finally:
            os.environ.pop(llm.DISABLE_ENV, None)
            tp.cleanup()

    def test_none_backend_disables_consolidation(self):
        tp = TempProject()
        try:
            res = consolidate.run(tp.cfg(), day="2026-09-02", backend=llm.NoneBackend(), out=lambda *_: None)
            self.assertEqual(res["result"], "no-backend")
        finally:
            tp.cleanup()

    def test_cli_digest_and_consolidate_dry_run_are_safe(self):
        tp = TempProject()
        try:
            r = run_cli("digest", "--dry-run", cfg_path=tp.cfg_path)
            self.assertEqual(r.returncode, 0, r.stderr)
            r = run_cli("profile", "consolidate", "--date", "2026-09-02", "--dry-run", cfg_path=tp.cfg_path)
            self.assertIn("backend is 'none'", r.stdout)
        finally:
            tp.cleanup()


if __name__ == "__main__":
    unittest.main()
