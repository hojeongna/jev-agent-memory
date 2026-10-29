"""`jevmem` command line.

Hook entries (`jevmem hook`, `jevmem capture inbound|outbound`) never exit non-zero and never
print errors: Claude Code treats exit code 2 from a UserPromptSubmit hook as "block this
prompt", so even a bad argument or a missing config file must not stop the conversation.
Those problems go to the per-user hook error log instead (see fsutil.hook_error_log).
"""
from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import sqlite3
import stat
import sys
import time
from datetime import datetime
from pathlib import Path

from . import __version__, config, capture, consolidate, digest, fsutil, hook, indexer, install, profile, raw, recall, schedule, status, store
from . import labeler
from . import llm as llmmod
from . import taxonomy as taxmod
from .jev_client import JevClient, JevError, load_api_key
from .text import clean_tokens, extract_query

EXAMPLE_TAXONOMY = Path(__file__).resolve().parent / "data" / "example_taxonomy.json"


def _cfg(args):
    return config.load(getattr(args, "config", None))


def _cfg_required(args):
    """Every command but `init` and `doctor` (search, raw, profile, capture tail/log-out,
    digest, index): without a config file they would show an empty memory, or write state
    files, rooted at whatever the current directory is, so they stop with exit 2 and say
    where they looked."""
    return config.load(getattr(args, "config", None), require_file=True)


def db_error_text(e: BaseException, path, kind: str) -> str:
    """What to tell a person when an SQLite database can't be used. kind: episodic | profile."""
    reason, damaged = store.db_problem(e)
    if not damaged:
        return f"{kind} DB {path}: {reason}"
    if kind == "episodic":
        return (f"episodic DB {path} is damaged ({reason}). It is rebuilt from your daily logs: move it aside "
                f"(mv {path} {path}.broken) and run `jevmem index` (every chunk is labeled again)")
    return (f"profile DB {path} is damaged ({reason}). It can't be rebuilt from the logs: restore it from a "
            f"backup, or move it aside (mv {path} {path}.broken) to start an empty profile")


def _hook_cfg(args):
    """Hook entries: a config FILE is required (never defaults rooted at the current dir)."""
    return config.load(getattr(args, "config", None), require_file=True)


def _err(e: BaseException) -> str:
    """Error text for stderr: the full message for a person at a terminal; for scheduled
    jobs (stderr goes to a log file) only the class name and a safe short reason."""
    try:
        tty = sys.stderr.isatty()
    except (AttributeError, ValueError):
        tty = False
    return f"{type(e).__name__}: {e}" if tty else fsutil.safe_reason(e)


# --------------------------------------------------------------------------- #
def cmd_init(args) -> int:
    target = Path(args.dir).expanduser().resolve()
    target.mkdir(parents=True, exist_ok=True)
    cfg_path = target / config.CONFIG_NAME
    if cfg_path.exists() and not args.force:
        print(f"{cfg_path} exists (use --force to overwrite the config file)")
    else:
        # 0600 when new (it may name your key file); an existing file keeps its mode.
        fsutil.write_text_atomic(cfg_path, config.TEMPLATE.format(logs_dir=args.logs))
        print(f"wrote {cfg_path}")
    cfg = config.load(str(cfg_path))
    fsutil.ensure_dir(cfg["data"]["dir"])
    tax = Path(cfg["data"]["taxonomy"])
    if not tax.exists():
        src = Path(args.taxonomy).expanduser() if args.taxonomy else EXAMPLE_TAXONOMY
        try:
            taxmod.load(src)
        except taxmod.TaxonomyError as e:
            print(f"error: {e}", file=sys.stderr)
            return 2
        fsutil.write_text_atomic(tax, src.read_text(encoding="utf-8"))
        print(f"wrote {tax} (example taxonomy — edit it to match your own logs; see docs/taxonomy.md)")
    con = store.connect(cfg["data"]["episodic_db"])
    store.init_db(con)
    con.close()
    pconn = profile.connect(Path(cfg["data"]["profile_db"]))
    profile.init_schema(cfg, pconn)
    pconn.close()
    logs = Path(cfg["logs"]["dir"])
    print(f"data dir: {cfg['data']['dir']}")
    print(f"logs dir: {logs}" + ("" if logs.exists() else "  (does not exist yet)"))
    print("next: edit the taxonomy, then `jevmem index --dry-run`, then `jevmem index`, "
          "then `jevmem install-hook`")
    return 0


def cmd_index(args) -> int:
    cfg = _cfg_required(args)
    try:
        if args.stats:
            print(json.dumps(indexer.stats(cfg), ensure_ascii=False, indent=2))
            return 0
        res = indexer.run(cfg, client_factory=lambda: JevClient.from_config(cfg), cap=args.cap,
                          workers=args.workers, relabel=args.relabel, relabel_topic=args.relabel_topic,
                          retry_failed=args.retry_failed, dry_run=args.dry_run,
                          no_label=args.no_label, yes=args.yes, force_prune=args.force_prune)
    except (taxmod.TaxonomyError, JevError) as e:
        print(f"error: {e}", file=sys.stderr)
        if not args.dry_run and not args.stats:
            status.record(cfg, "index", "error", fsutil.safe_reason(e))
        return 2
    except sqlite3.DatabaseError as e:
        print("error: " + db_error_text(e, cfg["data"]["episodic_db"], "episodic"), file=sys.stderr)
        if not args.dry_run and not args.stats:
            status.record(cfg, "index", "error", store.db_problem(e)[0])
        return 1
    if not args.dry_run:
        status.record(cfg, "index", indexer.status_word(res), indexer.status_detail(res))
    return 0 if not res.get("billing_stop") else 3


def cmd_digest(args) -> int:
    cfg = _cfg_required(args)
    if args.no_index:
        cfg["digest"]["index_after"] = False
    try:
        digest.run(cfg, dry_run=args.dry_run)
    except Exception as e:
        print(f"error: {_err(e)}", file=sys.stderr)
        if not args.dry_run:
            status.record(cfg, "digest", "error", fsutil.safe_reason(e))
        return 1
    return 0


def cmd_capture(args) -> int:
    """Hook entries never fail (exit 0, no output); log-out/tail are for humans/scripts."""
    if args.action in ("inbound", "outbound"):
        if os.environ.get(llmmod.DISABLE_ENV):
            return 0  # inside jevmem's own `claude -p` child
        try:
            payload = json.load(sys.stdin)
        except Exception:
            return 0  # empty / non-JSON stdin: nothing to record
        try:
            # A wrong value elsewhere in the config must not stop the chat log: see
            # config.load_for_capture ([data]/[capture] alone, the error is logged).
            cfg = config.load_for_capture(getattr(args, "config", None))
            (capture.inbound if args.action == "inbound" else capture.outbound)(cfg, payload)
            if cfg.partial_error:
                fsutil.hook_error_log(f"capture {args.action}: recorded using only [data] and [capture] of the "
                                      f"config, which has an error (ConfigError: {cfg.partial_error})")
        except Exception as e:
            fsutil.hook_error_log(f"capture {args.action}: not recorded ({fsutil.safe_reason(e)})")
        return 0
    cfg = _cfg_required(args)
    if args.action == "log-out":
        if not args.text:
            print("usage: jevmem capture log-out \"text\" [--channel C] [--chat-id ID]", file=sys.stderr)
            return 2
        capture.log_out(cfg, " ".join(args.text), channel=args.channel, chat_id=args.chat_id)
        return 0
    recs = capture.tail(cfg, args.n)
    if not recs:
        print(f"no records yet ({capture.log_path(cfg)})", file=sys.stderr)
    for r in recs:
        print(f"{r.get('ts', '')} {r.get('dir', '?'):>3} [{r.get('channel', '')}] "
              f"{capture.label(cfg, r)}: {' '.join(str(r.get('text', '')).split())[:160]}")
    return 0


def cmd_consolidate(args) -> int:
    cfg = _cfg_required(args)
    if args.date is not None:
        try:
            raw.check_date(args.date)
        except ValueError as e:
            print(f"error: {e}", file=sys.stderr)
            return 2
    try:
        res = consolidate.run(cfg, day=args.date, dry_run=args.dry_run, backfill=args.backfill)
    except sqlite3.DatabaseError as e:
        print("error: " + db_error_text(e, cfg["data"]["profile_db"], "profile"), file=sys.stderr)
        if not args.dry_run:
            status.record(cfg, "consolidate", "error", store.db_problem(e)[0])
        return 1
    except Exception as e:
        print(f"error: {_err(e)}", file=sys.stderr)
        if not args.dry_run:
            status.record(cfg, "consolidate", "error", fsutil.safe_reason(e))
        return 1
    return 0 if res.get("result") in ("ok", "dry-run", "no-log", "empty-log") else 1


def _require_config_file(args):
    """(config path, config) for install-hook / schedule, which write absolute paths and so
    need a real config file; (None, None) after printing why not. A file that was only found
    automatically must pass the ownership check, made on the very file that is read (the
    hooks and jobs will name it with --config, which is trusted as given)."""
    cfg_path, _ = config.find_config_how(getattr(args, "config", None))
    if cfg_path is None:
        print("no jevmem.toml found — run `jevmem init` first (or pass --config)", file=sys.stderr)
        return None, None
    try:
        return cfg_path, config.load(getattr(args, "config", None))
    except config.ConfigError as e:
        print(f"error: {e}", file=sys.stderr)
        return None, None


def cmd_schedule(args) -> int:
    cfg_path, cfg = _require_config_file(args)
    if cfg_path is None:
        return 2
    if args.action == "install":
        try:
            schedule.install(cfg, cfg_path, platform=args.platform, write=args.write, load=not args.no_load)
        except ValueError as e:
            print(f"error: {e}", file=sys.stderr)
            return 2
    elif args.action == "uninstall":
        schedule.uninstall(cfg, cfg_path, platform=args.platform, write=args.write)
    else:
        print(json.dumps(schedule.installed(cfg, cfg_path, platform=args.platform), indent=2))
    return 0


def cmd_hook(args) -> int:
    return hook.main(lambda: _hook_cfg(args))


def cmd_install_hook(args) -> int:
    cfg_path, cfg = _require_config_file(args)
    if cfg_path is None:
        return 2
    target = install.settings_path(Path(args.project).expanduser(), args.scope)
    try:
        res = install.install(Path(args.project).expanduser(), cfg_path, scope=args.scope,
                              dry_run=args.dry_run, uninstall=args.uninstall,
                              reply_tools=cfg["capture"]["reply_tools"], capture_hooks=not args.no_capture)
    except json.JSONDecodeError as e:
        print(f"error: {target} is not valid JSON ({e}); fix or move it, then run install-hook again",
              file=sys.stderr)
        return 2
    except ValueError as e:
        print(f"error: {target}: {e}", file=sys.stderr)
        return 2
    if not args.dry_run:
        install.record_settings_path(cfg, res["settings"], removed=args.uninstall)
    if not args.uninstall:
        print()
        print(install.schedule_help(cfg_path))
    return 0


def cmd_profile(args) -> int:
    if getattr(args, "consolidate", False):
        return cmd_consolidate(args)
    cfg = _cfg_required(args)
    try:
        return args.pfunc(cfg, args)
    except sqlite3.DatabaseError as e:
        print("error: " + db_error_text(e, profile.db_path(cfg, args), "profile"), file=sys.stderr)
        return 1


def cmd_search(args) -> int:
    cfg = _cfg_required(args)
    query = extract_query(args.query)
    tokens = clean_tokens(query, rare_normalize=cfg["recall"]["rare_normalize"])
    out = {"tokens": tokens}
    def fts() -> int:
        hits = recall.fts_fallback(cfg, tokens)
        out["fts_hits"] = [{"date": h["date"], "relpath": h["relpath"], "score": round(h["score"], 3)} for h in hits]
        print(json.dumps(out, ensure_ascii=False, indent=2))
        if hits:
            print(recall.render_hits(cfg, cfg["fallback"]["header"], hits, cfg["hook"]["max_chunk_print"]))
        return 0

    if args.fts:
        return fts()
    try:
        tax = taxmod.load(cfg["data"]["taxonomy"])
    except taxmod.TaxonomyError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    try:
        client = JevClient.from_config(cfg)
    except JevError:
        print(f"no Jev API key (${cfg['jev']['api_key_env']} or [jev].api_key_file): showing the FTS "
              "keyword fallback instead (same as --fts)", file=sys.stderr)
        out["fallback"] = "no Jev API key"
        return fts()
    prior = [f"{cfg['context']['user_label']}: {line}" for line in (args.context or [])]
    info, calls = {}, []
    try:
        hits, _, _ = recall.jev_recall(cfg, tax, client, lambda: recall.load_units(cfg), args.query,
                                       tokens, prior, recall.Deadline(args.deadline), None, info, calls)
    except Exception as e:
        out.update(info, error=f"{type(e).__name__}: {e}", calls=calls)
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return 1
    out.update(info, calls=calls)
    print(json.dumps(out, ensure_ascii=False, indent=2))
    if hits:
        print(recall.render_hits(cfg, cfg["recall"]["header"], hits, cfg["hook"]["max_chunk_print"]))
    return 0


def cmd_raw(args) -> int:
    cfg = _cfg_required(args)
    rest = list(args.rest or [])
    hhmm = rest.pop(0) if rest and raw.TIME_RE.match(rest[0]) else None
    try:
        lines = raw.lookup(cfg, args.date, hhmm, rest, around=args.around, max_lines=args.max)
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    print("\n".join(lines))
    return 0


def cmd_doctor(args) -> int:
    ok = True

    def line(good, msg):
        nonlocal ok
        ok = ok and good
        print(("ok   " if good else "FAIL ") + msg)

    try:
        cfg = _cfg(args)
        line(cfg.source is not None, f"config: {cfg.source or '(none found; using defaults)'}"
             + ("" if cfg.source else f" — searched {config.SEARCH_ORDER}"))
        for k in getattr(cfg, "unknown", []):
            print(f"warn config: unknown {k} in {cfg.source} (a typo? it is ignored)")
    except config.ConfigError as e:
        line(False, f"config: {e}")
        try:
            cfg = config.load_for_capture(getattr(args, "config", None))
            print("warn hooks: until the config is fixed, the recall hook injects nothing; chat capture goes on "
                  "using only [data] and [capture] of the file (the checks below use those and the defaults)")
        except config.ConfigError:
            cfg = config.defaults()
            line(False, "hooks: until the config is fixed, the hooks record no chat and recall nothing "
                 f"(the checks below use the defaults, rooted at {cfg.root})")
    logs = Path(cfg["logs"]["dir"])
    n = len([p for p in logs.glob(cfg["logs"]["glob"]) if p.is_file()]) if logs.is_dir() else 0
    st, st_err = None, None
    try:
        st = indexer.stats(cfg)
    except (sqlite3.DatabaseError, OSError) as e:
        st_err = e
    units = st["units"] if st else 0
    try:
        others = logs.is_dir() and any(True for _ in logs.iterdir())
    except OSError:
        others = True  # unreadable: not "empty"
    if logs.exists() and not logs.is_dir():
        line(False, f"logs: {logs} is not a directory (check [logs] dir)")
    elif n > 0:
        line(True, f"logs: {logs} ({n} files matching {cfg['logs']['glob']})")
    elif units == 0 and st_err is None and not others:
        print(f"info logs: {logs} has no daily log yet{'' if logs.is_dir() else ' (the folder does not exist)'} — "
              "the first digest summary creates it")
    else:
        line(False, f"logs: {logs} (0 files matching {cfg['logs']['glob']}"
             + ("; the folder holds other files: check [logs] glob)" if others else ")"))
    try:
        tax = taxmod.load(cfg["data"]["taxonomy"])
        w = taxmod.validate(tax)
        line(True, f"taxonomy: {cfg['data']['taxonomy']}")
        if taxmod.is_example(tax):
            print("warn taxonomy: example taxonomy in use (from `jevmem init`) — routing quality depends on "
                  "a taxonomy written for your own logs; see docs/taxonomy.md")
        for x in w:
            print("warn " + x)
    except Exception as e:
        line(False, f"taxonomy: {e}")
    try:
        load_api_key(cfg)
        line(True, f"api key: found (${cfg['jev']['api_key_env']} or api_key_file)")
        kf = cfg["jev"].get("api_key_file")
        if kf and not os.environ.get(cfg["jev"].get("api_key_env") or "TYPESAFE_API_KEY", "").strip():
            try:
                kmode = stat.S_IMODE(Path(kf).expanduser().stat().st_mode)
                if kmode & 0o077:
                    print(f"warn api key file {kf} is readable by others (mode {oct(kmode)}): chmod 600 {kf}")
            except OSError:
                pass
    except JevError as e:
        line(False, f"api key: {e} — the hook will use the FTS fallback only")
    until = hook.breaker_until(cfg)
    if until:
        print(f"warn jev: paused until {until} after an HTTP 402 (billing) answer; recall uses "
              "the FTS fallback meanwhile")
    if st_err is not None:
        line(False, "episodic index: " + db_error_text(st_err, cfg["data"]["episodic_db"], "episodic"))
    elif st["units"] > 0 and not st["labeled"]:
        print(f"warn episodic index: {st['units']} chunks, 0 labeled — Jev recall needs labeled chunks "
              "(until then the hook uses the FTS fallback); run `jevmem index`")
    elif st["units"] == 0 and n == 0:
        print("info episodic index: empty (nothing to index yet)")
    elif st["units"] == 0:
        line(False, f"episodic index: 0 chunks — run `jevmem index` for the {n} log files")
    else:
        line(True, f"episodic index: {st['units']} chunks, {st['labeled']} labeled")
    if st and st.get("gave_up"):
        print(f"warn labeling: {st['gave_up']} chunk(s) given up after {cfg['index']['max_tries']} failed "
              "attempts — fix the labeler (see below), then `jevmem index --retry-failed`")
    pdb = Path(cfg["data"]["profile_db"])
    try:
        n_traits = profile.active_count(cfg)
        if n_traits is None:
            print(f"info profile: no profile DB yet ({pdb}; `jevmem init` creates it)")
        else:
            line(True, f"profile: {n_traits} active trait(s)")
    except (sqlite3.DatabaseError, OSError) as e:
        line(False, "profile: " + db_error_text(e, pdb, "profile"))

    hook_cmds = install.find_hook_commands(cfg)
    capture_hooked = any(f" -m jevmem {k} " in c + " " for _, cmds in hook_cmds for c in cmds
                         for k in install.CAPTURE_KEYS)
    cp = capture.log_path(cfg)
    recs = capture.tail(cfg, 1)
    if recs:
        line(True, f"capture log: {cp} (last {recs[-1].get('ts')})")
    elif cp.exists():
        line(True, f"capture log: {cp} (no complete record yet)")
    elif capture_hooked:
        print(f"info capture log: {cp} — nothing captured yet; the capture hooks are installed, so send a "
              "message through the chat channel")
    elif cfg["digest"]["source"] == "capture":
        line(False, f"capture log: {cp}  (empty — `jevmem install-hook` adds the capture hooks)")
    else:
        print(f"info capture log: not used for summaries ([digest] source = \"{cfg['digest']['source']}\")")
    bs = llmmod.backends(cfg)
    if not bs:
        line(False, "llm backend: none — digest writes raw lines, consolidation is disabled")
    for i, b in enumerate(bs):
        good, msg = b.check()
        role = "primary" if i == 0 else "fallback"
        (line if i == 0 else (lambda g, m: print(("ok   " if g else "warn ") + m)))(
            good, f"llm {role} ({b.name}): {msg}")
    uses_claude = any(b.name == "claude" for b in bs) or cfg["index"].get("labeler", "haiku") == "haiku"
    if cfg["index"].get("labeler", "haiku") == "haiku":
        good, msg = labeler.default_backend(cfg).check()
        line(good, f"labeler: haiku ({msg}; `claude --version` only, login status not checked)")
    else:
        print("info labeler: jev (see `jevmem index --dry-run`)")
    if uses_claude:
        ignored = llmmod.ignored_api_key_envs(cfg)
        if ignored:
            print(f"info claude -p: {', '.join('$' + k for k in ignored)} is set but ignored — claude -p "
                  "uses the login of your claude CLI ([llm] claude_use_api_key = true passes the key on)")
        brk = llmmod.breaker(cfg)
        paused = brk.blocked(persist=False)
        if paused:
            print(f"warn claude -p: {llmmod.pause_text(paused)}; jevmem retries automatically after that")
        elif llmmod.CallBreaker.failures_of(brk.state()):
            s = brk.state()
            print(f"info claude -p: the last {llmmod.CallBreaker.failures_of(s)} call(s) failed "
                  f"({s.get('reason', '?')}); the next scheduled run tries again")
        lc = cfg["llm"]
        if lc.get("claude_tools_off") is False and not llmmod.tool_args(lc.get("claude_disallowed_tools")):
            print("warn claude -p: claude_tools_off = false and claude_disallowed_tools = \"\": the background "
                  "claude -p children may use every tool your claude settings allow, on chat text anyone in "
                  "the chat could have written")
    dc = cfg["digest"]
    if dc["source"] == "claude_transcript":
        n = len(digest.source_files(cfg))
        line(n > 0, f"digest source: claude_transcript {digest.transcript_dir(cfg)} ({n} session files)")
    elif dc["source"] == "jsonl":
        problem = digest.source_problem(cfg)
        line(problem is None, f"digest source: jsonl {dc.get('jsonl_path') or ''}".rstrip()
             + (f" — {problem}" if problem else ""))
    else:
        print("info digest source: capture log")
    ctx = cfg["context"]
    if ctx["source"] == "jsonl" and not (ctx.get("jsonl_path") and Path(ctx["jsonl_path"]).is_file()):
        print(f"warn context source: jsonl, but [context] jsonl_path {ctx.get('jsonl_path') or 'is not set'}"
              f"{' does not exist' if ctx.get('jsonl_path') else ''} — recall gets no recent chat lines")
    found_hooks = False
    for sp, cmds in hook_cmds:
        for py in sorted({install.command_python(c) or "?" for c in cmds}):
            found_hooks = True
            line(Path(py).is_file(), f"hook interpreter ({install.short_path(sp, cfg.root)}): {py}" +
                 ("" if Path(py).is_file() else "  (missing — run `jevmem install-hook` again)"))
    if not found_hooks:
        print(f"info hooks: no jevmem hooks for this config in {cfg.root / '.claude'}, ./.claude, "
              "~/.claude/settings.json or where `jevmem install-hook --project` wrote them")
    if cfg.source is not None:
        inst = schedule.installed(cfg, cfg.source)
        line(all(inst.values()), "schedule: " + ", ".join(f"{k}={'yes' if v else 'no'}" for k, v in inst.items())
             + ("" if all(inst.values()) else "  (jevmem schedule install --write)"))
    hel = fsutil.hook_error_log_path()
    if hel.exists():
        try:
            last = hel.read_text(encoding="utf-8", errors="replace").strip().splitlines()[-1:]
        except OSError:
            last = []
        if last:
            # Lines start with an ISO time; an entry older than a day is shown as info (it may
            # have been fixed since). Deleting the file clears it.
            try:
                age = time.time() - datetime.fromisoformat(last[0].split(" ", 1)[0]).timestamp()
            except (ValueError, OverflowError, OSError):
                age = 0.0
            word = "info" if age > 86400 else "warn"
            print(f"{word} hook error log {hel} (all projects; delete it to clear), last entry: {last[0][:200]}")
    runs = status.read(cfg)
    for job in ("digest", "index", "consolidate"):
        r = runs.get(job)
        print(f"info last {job}: " + (f"{r['ts']} {r['result']} {r.get('detail', '')}".rstrip() if r else "never"))
    return 0 if ok else 1


# --------------------------------------------------------------------------- #
def build_parser() -> argparse.ArgumentParser:
    help_cfg = ("path to jevmem.toml. Without it: $JEVMEM_CONFIG, then ./jevmem.toml, then "
                "~/.config/jevmem/jevmem.toml")
    # Subcommands use SUPPRESS so `jevmem --config X index` is not reset to None.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--config", default=argparse.SUPPRESS, help=help_cfg)
    p = argparse.ArgumentParser(prog="jevmem", description="Jev-routed agent memory for Claude Code")
    p.add_argument("--config", default=None, help=help_cfg)
    p.add_argument("--version", action="version", version=f"jevmem {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("init", help="create jevmem.toml, data dir, example taxonomy, DBs", parents=[common])
    sp.add_argument("--dir", default=".", help="where to write jevmem.toml (default: .)")
    sp.add_argument("--logs", default="memory", help="daily logs dir, relative to --dir (default: memory)")
    sp.add_argument("--taxonomy", default=None, help="start from this taxonomy JSON instead of the example")
    sp.add_argument("--force", action="store_true", help="overwrite an existing jevmem.toml")
    sp.set_defaults(func=cmd_init)

    sp = sub.add_parser("index", help="chunk logs and label new/changed chunks", parents=[common])
    sp.add_argument("--dry-run", action="store_true",
                    help="show chunk and call counts; no writes, no API or claude calls")
    sp.add_argument("--cap", type=int, default=None, help="max chunks to label this run")
    sp.add_argument("--workers", type=int, default=None)
    sp.add_argument("--relabel", action="store_true", help="mark ALL chunks stale (after a taxonomy change)")
    sp.add_argument("--relabel-topic", default=None, help="mark chunks labeled with this top-level topic stale")
    sp.add_argument("--retry-failed", action="store_true", help="retry chunks that hit max_tries")
    sp.add_argument("--no-label", action="store_true", help="only chunk + FTS index (no labeling calls)")
    sp.add_argument("--yes", action="store_true", help="lift the per-run / per-day Jev labeling call budget limits")
    sp.add_argument("--force-prune", action="store_true",
                    help="delete indexed chunks even if the logs folder is missing or no file matches "
                         "(without it such a run keeps everything)")
    sp.add_argument("--stats", action="store_true", help="print index stats and exit")
    sp.set_defaults(func=cmd_index)

    sp = sub.add_parser("hook", help="UserPromptSubmit hook entry (stdin JSON -> stdout)", parents=[common])
    sp.set_defaults(func=cmd_hook)

    sp = sub.add_parser("install-hook", help="add capture + recall hooks to .claude/settings.json (backup first)",
                        parents=[common])
    sp.add_argument("--project", default=".", help="project directory (default: .)")
    sp.add_argument("--scope", choices=["project", "local", "user"], default="project",
                    help="project=.claude/settings.json, local=.claude/settings.local.json, "
                         "user=~/.claude/settings.json")
    sp.add_argument("--dry-run", action="store_true")
    sp.add_argument("--uninstall", action="store_true")
    sp.add_argument("--no-capture", action="store_true",
                    help="only the recall hook: no chat capture hooks (removes jevmem capture hooks already there)")
    sp.set_defaults(func=cmd_install_hook)

    sp = sub.add_parser("capture", help="stage 1: chat-channel log (hook entries, log-out, tail)",
                        parents=[common])
    sp.add_argument("action", choices=["inbound", "outbound", "log-out", "tail"],
                    help="inbound/outbound: hook entries (stdin JSON); log-out: record a message "
                         "a script sent; tail: show recent records")
    sp.add_argument("text", nargs="*", help="log-out: the message text")
    sp.add_argument("--channel", default="script")
    sp.add_argument("--chat-id", default="")
    sp.add_argument("-n", type=int, default=20)
    sp.set_defaults(func=cmd_capture)

    sp = sub.add_parser("digest", help="stage 2: summarize new chat into the daily log, then label new chunks",
                        parents=[common])
    sp.add_argument("--dry-run", action="store_true",
                    help="show how many new messages would be summarized and where; never calls the LLM, "
                         "writes nothing, doesn't label")
    sp.add_argument("--no-index", action="store_true", help="skip the labeling step this run")
    sp.set_defaults(func=cmd_digest)

    sp = sub.add_parser("schedule", help="run digest + daily consolidation unattended (launchd or cron)",
                        parents=[common])
    sp.add_argument("action", choices=["install", "uninstall", "status"])
    sp.add_argument("--write", action="store_true", help="actually write (default: print only)")
    sp.add_argument("--platform", choices=["macos", "linux"], default=None)
    sp.add_argument("--no-load", action="store_true", help="macOS: write plists without launchctl load")
    sp.set_defaults(func=cmd_schedule)

    sp = sub.add_parser("search", help="debug: run the recall pipeline for a query", parents=[common])
    sp.add_argument("query")
    sp.add_argument("--fts", action="store_true",
                    help="keyword fallback only (no API call; also used when no Jev key is configured)")
    sp.add_argument("--context", action="append", help="add a prior chat line (repeatable)")
    sp.add_argument("--deadline", type=float, default=30.0)
    sp.set_defaults(func=cmd_search)

    sp = sub.add_parser("raw", help="show the original chat lines behind a summary (the recall pointer)",
                        parents=[common],
                        description="jevmem raw DATE [HH:MM] [KEYWORD ...]: the chat lines the "
                                    "'## HH:MM summary' of DATE was written from. With keywords, only "
                                    "matching lines (±1 line of context). Without a time, the whole day.")
    sp.add_argument("date", help="YYYY-MM-DD (the daily log's date)")
    sp.add_argument("rest", nargs="*", metavar="HH:MM|KEYWORD",
                    help="optional summary time (HH:MM), then optional keywords")
    sp.add_argument("--around", default=None, metavar="HH:MM",
                    help="the summary window that contains this time")
    sp.add_argument("--max", type=int, default=None, help="print at most this many lines")
    sp.set_defaults(func=cmd_raw)

    sp = sub.add_parser("doctor", help="check config, logs, taxonomy, key, index", parents=[common])
    sp.set_defaults(func=cmd_doctor)

    # --config is accepted before or after every profile subcommand.
    profile.add_parser(sub, parents=[common])
    sub.choices["profile"].set_defaults(func=cmd_profile)
    return p


def _hook_entry(argv: list[str]) -> str | None:
    """"hook" / "capture inbound" / "capture outbound" (the commands install-hook writes),
    else None."""
    pos, skip = [], False
    for tok in argv:
        if skip:
            skip = False
        elif tok == "--config":
            skip = True
        elif not tok.startswith("-"):
            pos.append(tok)
            if len(pos) == 2:
                break
    if pos[:1] == ["hook"]:
        return "hook"
    if pos[:1] == ["capture"] and pos[1:2] in (["inbound"], ["outbound"]):
        return " ".join(pos[:2])
    return None


def _isatty(stream) -> bool:
    try:
        return bool(stream.isatty())
    except (AttributeError, ValueError):
        return False


def _wants_help(argv: list[str]) -> bool:
    """A person asked for help: -h/--help given explicitly, or nothing is piped into stdin
    (Claude Code always pipes the hook JSON; a terminal would just wait forever)."""
    skip = False
    for tok in argv:
        if skip:
            skip = False
        elif tok == "--config":
            skip = True
        elif tok in ("-h", "--help"):
            return True
    return _isatty(sys.stdin)


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    parser = build_parser()
    hook_entry = _hook_entry(argv)
    if hook_entry and _wants_help(argv):
        try:
            parser.parse_args([a for a in argv if a not in ("-h", "--help")] + ["--help"])
        except SystemExit:
            pass
        return 0
    if not hook_entry:
        args = parser.parse_args(argv)
        try:
            return args.func(args) or 0
        except config.ConfigError as e:
            print(f"error: {e}", file=sys.stderr)
            return 2
        except sqlite3.DatabaseError as e:  # commands without their own message for it
            print(f"error: a jevmem database can't be used ({store.db_problem(e)[0]}); "
                  "`jevmem doctor` names the file and what to do", file=sys.stderr)
            return 1
    # Hook entry: a parse error must not become exit code 2 (that would block the prompt),
    # and nothing may be printed (stdout of a UserPromptSubmit hook goes into the prompt).
    err = io.StringIO()
    try:
        with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
            args = parser.parse_args(argv)
    except SystemExit as e:
        if e.code not in (0, None):
            detail = (err.getvalue().strip().splitlines() or ["?"])[-1]
            fsutil.hook_error_log(f"{hook_entry}: bad arguments, nothing done ({detail[:200]})")
        return 0
    try:
        args.func(args)
    except Exception as e:
        fsutil.hook_error_log(f"{hook_entry}: {fsutil.safe_reason(e)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
