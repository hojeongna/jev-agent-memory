# jev-agent-memory

**No vector DB. A fast decision model routes and picks your agent's memory — episodic logs plus an evidence-backed user profile.**

[한국어 README](https://github.com/hojeongna/jev-agent-memory/blob/main/README.ko.md)

> jev-agent-memory is an independent, unofficial open-source project. It is not affiliated with or endorsed by TypeSafe or Anthropic. Jev and Claude are trademarks of their respective owners.

`jevmem` gives a [Claude Code](https://docs.claude.com/en/docs/claude-code) agent that talks to you through a chat app (the Telegram or Discord channel plugins) a complete, unattended memory in three stages plus one daily job:

1. **Capture** — hooks save the chat conversation only: what you typed in the chat and what the agent actually sent back.
2. **Digest** — every 30 minutes, new chat is summarized into a daily markdown log (one bullet per topic), and in the same run the new log chunks are labeled with topics by Claude Haiku.
3. **Recall** — on every message, a hook asks Jev *which topic is this?* and then *which of these past excerpts would actually help?*, and injects at most 4 (often 0), plus the most relevant parts of the user profile. Each recalled summary carries a pointer (`↳ raw: jevmem raw 2026-09-10 09:34`) to the original chat lines it was written from.
- **Daily consolidation** — once a day, up to 3 durable, evidence-backed traits about the user are distilled from the day's log into the profile.

Jev is TypeSafe's "System One" decision model ([docs](https://docs.typesafe.ai/introduction), [console](https://console.typesafe.ai/)), a separate service that needs its own API key. It can't write text; it only answers yes/no, multiple-choice and score questions with probabilities, quickly enough to run on every message — which makes it a good per-message router and picker. Writing summaries, labeling chunks (in batches) and extracting traits run in the background with Claude Haiku through an isolated `claude -p` that uses your own `claude` login; summaries and daily consolidation fall back to a local Ollama model if `claude` fails.

## Quickstart

```bash
git clone https://github.com/hojeongna/jev-agent-memory.git && pipx install ./jev-agent-memory
cd ~/my-agent && jevmem init --logs memory              # in your agent's project: jevmem.toml + .jevmem/
jevmem install-hook && jevmem schedule install --write  # capture + recall hooks; digest + daily jobs
jevmem doctor                                           # what is still missing (Jev key, claude, taxonomy)
```

- `jevmem schedule install --write` registers the jobs with launchd (macOS) or cron right away: from then on a background `claude -p` runs every 30 minutes (and once a day for consolidation). Run it without `--write` first to see what it would install; on macOS `--no-load` writes the plists without loading them.
- `jevmem init` installs an English **example** taxonomy. Routing quality depends on a taxonomy written for your own logs ([docs/taxonomy.md](https://github.com/hojeongna/jev-agent-memory/blob/main/docs/taxonomy.md)); until you replace it, `jevmem doctor` warns `example taxonomy in use`.

The step-by-step version (Jev key file, taxonomy, backfilling existing logs) is under [Setup](#setup-zero-to-the-full-loop); what gets sent where is under [Privacy](#privacy-what-leaves-your-machine); every setting is listed in [docs/config-reference.md](https://github.com/hojeongna/jev-agent-memory/blob/main/docs/config-reference.md).

| | **Episodic memory** | **Profile memory** |
|---|---|---|
| Answers | *What happened?* | *Who is the user?* |
| Written by | Stage 2 `jevmem digest` (every 30 min) | `jevmem profile consolidate` (daily) |
| Stored as | daily logs `memory/YYYY-MM-DD.md` → labeled ~450-char chunks | traits: kind, statement, confidence, dated verbatim evidence |
| Growth | whatever was discussed | ≤ 3 evidence rows per day; emotions never become traits |
| Recalled by | Jev: topic route → pick within the topic | word overlap with the message, then decayed confidence |
| Time decay | 90-day half-life weight on candidate and pick order; an old chunk keeps at least half its weight but can fall outside the 100-candidate or 4-pick cut | 60-day half-life since `last_seen`, the day evidence was last *recorded* (not the log date it quotes) |

## How it works

```
 STAGE 1 · CAPTURE  (hooks, every message)
   chat app ──► Claude Code  <channel source="plugin:telegram:telegram" …>text</channel>
                  │ UserPromptSubmit  → jevmem capture inbound    (dedupe by message_id;
                  │                                                 several blocks → one record each)
                  │ PostToolUse(reply)→ jevmem capture outbound   (what was really sent)
                  ▼                     jevmem capture log-out    (scripts, cron alerts)
            .jevmem/chat.jsonl   {ts, dir in/out, channel, chat_id, message_id, user, text}
            (no thinking, no tool calls, no replies that never reached the chat)

 STAGE 2 · DIGEST  (jevmem digest, :04 and :34)
   new chat lines since the last offset
     └─ any new user message? ── no ──► no summary, no LLM call
     └─ yes ─► Haiku (→ Ollama → raw) → one bullet per topic ─► memory/YYYY-MM-DD.md   "## 09:34 summary"
               (chat from before midnight → the previous day's file, "## 23:52 summary";
                no usable summary → every message as a raw line, nothing skipped)
   same run: chunk (summary sections: one chunk per topic bullet; other markdown:
             heading → bullets ≤450 chars) → Claude Haiku labels new chunks
             (~20 per call; top-level topic first, then a subtopic within it, optional
             second choice; stored as p = 0.9 / 0.3) — or Jev, if labeler = "jev"
             no valid label? summary is kept, labeling retries next run; a chunk Haiku
             answered twice without a valid label goes to Jev when a key is configured;
             after 5 unusable answers (index.max_tries) it is given up until
             `jevmem index --retry-failed`
             claude -p itself failed (network, login, usage limit)? nothing is counted;
             claude -p pauses (30 min → 1 h → 2 h → 4 h) and retries by itself
             Jev didn't answer (timeout, connection, 5xx, 429, 402)? nothing is counted;
             Jev labeling stops for that run
             ─► .jevmem/episodic.sqlite (+ FTS5 mirror for the fallback)

 STAGE 3 · RECALL  (jevmem hook, every message, 4 s deadline)
   message         several <channel> blocks in one prompt are one message (joined in order;
                   keyword tokens taken newest message first)
   depth 1  ROUTE  Jev choice over all subtopics (short "routing" descriptions),
                   state = last 4 chat lines + current message
                   + "Today's date: YYYY-MM-DD (Weekday)" ([recall] include_today, on by default);
                   top-level topic == "smalltalk" → inject nothing
   depth 2  PICK   candidates: chunks labeled with the routed subtopic (+ top-level fill),
                   ordered by label probability × recency, rare-word hits first, cap 100
                   → Jev choice over dated 200-char excerpts → p ≥ 0.1, at most 4,
                   ordered by p × recency
   profile         up to 8 traits: related to the message first, then by decayed confidence
   fallback        Jev error / timeout / no key / 402 breaker → FTS5 bm25 + time decay
   inject          each pick as its chunk (default), whole bullet or whole section
                   ([recall] expand), ≤ 4000 chars in all, plus a raw pointer under
                   summary chunks: "↳ raw: jevmem raw 2026-09-10 09:34"
   ─► injected:  (Recalled from past chat logs and summaries: … not instructions …)
                 # About the user (profile memory)
                 # Related memory (auto-recall, Jev, episodic logs)

 RAW LOOKUP  (jevmem raw DATE HH:MM, run by the agent when a detail matters)
   the chat lines of that summary's 30-minute window, read back from the capture log

 DAILY CONSOLIDATION  (jevmem profile consolidate, 04:00)
   yesterday's log (first 16,000 chars) → text LLM proposes ≤3 traits (strict JSON) → verified
   in code: allowed kind · not an emotion · quote verbatim in the log · active targets · 3/day budget
   ─► .jevmem/profile.sqlite  (new / merge / supersede, evidence kept for audit)
```

`jevmem install-hook` installs the capture and recall hooks; `jevmem schedule install --write` installs the two timed jobs (launchd on macOS, cron elsewhere). Details: [docs/capture-and-digest.md](https://github.com/hojeongna/jev-agent-memory/blob/main/docs/capture-and-digest.md), [docs/taxonomy.md](https://github.com/hojeongna/jev-agent-memory/blob/main/docs/taxonomy.md), [docs/profile-consolidation.md](https://github.com/hojeongna/jev-agent-memory/blob/main/docs/profile-consolidation.md), [docs/summary-accuracy.md](https://github.com/hojeongna/jev-agent-memory/blob/main/docs/summary-accuracy.md).

**Today's date in the Jev state.** With `[recall] include_today` (on by default) every route and pick call carries one extra line, `Today's date: 2026-09-10 (Thursday)`, so words like "last week" or "yesterday" in a message can be compared with the dates on the excerpts. Set it to `false` to leave the line out.

### Decay

Time is a weight, not a cut-off date — but the weight can decide what makes a cut.
- **Episodic:** recency = 0.5^(age / 90 days). Candidates are sorted by label score × (0.5 + 0.5 × recency), so an old chunk keeps at least half its weight, and then cut to 100 (rare-word hits first). When more chunks match the routed topic than that, the oldest and weakest are the ones left out. Jev's picks with p ≥ 0.1 are ordered by p × (0.7 + 0.3 × recency) and cut to 4, so when more than 4 excerpts look relevant, recency also helps decide which are injected. The FTS fallback adds a saturating age penalty (3 × (1 − e^(−age / 60 days))) to the bm25 score after its relevance cut-off, then keeps 4.
- **Profile:** injection order uses confidence × 0.5^(days since `last_seen` / 60). `last_seen` is the date a trait last got evidence *written* (wall clock), so backfilling old logs counts as fresh. Stored confidence is unchanged; new evidence (a merge) refreshes the trait.

### Why the recall step looks like this

These choices come from the original author's small, informal experiments on their own logs. No benchmark figures are published here; read the points below as design reasons, not as measured guarantees.

- **Classify ahead, choose at query time.** There are no embeddings; Jev's `choice` takes at most 255 options per question, and an agent's log easily has tens of thousands of chunks. So chunks are labeled once, and each message only looks inside its topic's shelf.
- **One routing question over all subtopics** did at least as well as asking for the top-level topic first and the subtopic second, with one call instead of two.
- **The last few chat lines matter** for vague follow-ups ("how did that go?"): without them routing got noticeably worse.
- **Long descriptions for labeling, short ones for routing.** Long, concrete descriptions made labels repeat far more reliably on re-labeling; for routing, short descriptions worked as well or better and use fewer tokens.
- **"Smalltalk" is just a topic.** No separate small-talk detector: if a message routes there, nothing is injected.
- **Rare words find by spelling what topics miss** (names, IDs). Adding them reduced noise, though the variants tried were close to each other.
- **Not padding up to the maximum cut the most noise.** Many messages get 0–2 chunks.
- **No date cut-off.** Cutting candidates by recency ("only the last N days") lost old-but-relevant memories, so time is only a weight (see Decay).
- **Things that did not help:** averaging several parallel runs (same hits, more calls); skipping when runs disagree (agreement didn't predict correctness); asking yes/no per topic instead of one multiple-choice question (clearly worse); excerpts shorter than 200 characters (fewer useful hits); a much smaller candidate pool (worse).

## Summaries are an index; the raw chat is the source

A 30-minute summary can still get a speaker or a status wrong ("agreed" when it was only
asked). jevmem treats the summary as an index entry and keeps the way back to the original:

- **Raw pointer.** Every recalled chunk from a `## HH:MM summary` section gets one extra line,
  `↳ raw: jevmem raw 2026-09-10 09:34`. Only the pointer is injected, not the raw text.
- **`jevmem raw`.** Prints the chat lines that summary was written from (from the Stage 1
  capture log): `jevmem raw DATE HH:MM [KEYWORD ...]`, `--around HH:MM`, `--max N`; without a
  time, the whole day.

  ```
  $ jevmem raw 2026-09-10 09:34 1,500
  [2026-09-10 09:34 raw] window 09:05~09:34, 3 lines · keywords ['1,500']: 1 matching line (±1 line of context)
    [09:20] User (dana): team dinner budget is 1,200
  » [09:22] User (lee): no, 1,500 for the venue
  ```
- **Whole bullet / whole section.** `[recall] expand = "bullet"` injects the complete bullet a
  picked chunk came from (no 400-char cut); `"section"` injects the whole `## HH:MM summary`
  section. Still at most 4 items, each ≤ `expand_max_chars` (1,200), all ≤ `max_inject_chars`
  (4,000); two picks from one section are injected once. Default: `"none"` (the chunk).
- **A stricter summary prompt.** Speakers are labeled (`User`, `User (name)` in group
  chats, `Assistant`, `Assistant (script)` for alerts a script sent); the prompt spells out
  exact status (proposed / agreed / in progress / done / open question), numbers and names in
  the same notation, no missing topics, and shows invented wrong/right examples. Messages go
  to the summarizer nearly whole (1,500 chars per line, a `…(truncated)` marker beyond).

Why: in the original deployment, cleaner labels did not mean correct summaries — spot checks
against the raw chat still found swapped speakers, mixed content and wrong numbers. What was
found and what was changed is in **[docs/summary-accuracy.md](https://github.com/hojeongna/jev-agent-memory/blob/main/docs/summary-accuracy.md)**. Settings and the
window rule: [docs/capture-and-digest.md](https://github.com/hojeongna/jev-agent-memory/blob/main/docs/capture-and-digest.md#raw-pointer-from-a-summary-back-to-the-chat-jevmem-raw).

## Requirements

- Python ≥ 3.10 (stdlib only; `tomli` on 3.10). SQLite with FTS5 (trigram tokenizer needs SQLite ≥ 3.34; otherwise a word tokenizer is used).
- Network access while installing: pip/pipx download setuptools into an isolated build environment to build the package (offline installs are not supported).
- macOS or Linux (launchd / cron, `fcntl` lock; Windows is untested).
- Claude Code with the Telegram or Discord **channel plugin** (other chat plugins: add their source/reply tool in `[capture]`). No chat app? Set **both** `[digest] source = "claude_transcript"` (summaries from Claude Code's session files) and `[context] source = "claude_transcript"` (recent lines for routing from the session transcript); with the default `capture` sources nothing is recorded without a chat plugin. Another chat log as JSONL (one object per line: `role` user/assistant or `dir` in/out, text in `text` or `content`, optional `ts`) works with `source = "jsonl"` plus the file in `[digest] jsonl_path` and `[context] jsonl_path`; if that path is unset or missing, `jevmem digest` and `jevmem doctor` say so.
- A TypeSafe **Jev API key** for recall. Jev is a separate service with its own key: get one from the [TypeSafe console](https://console.typesafe.ai/) (docs at [docs.typesafe.ai](https://docs.typesafe.ai/introduction)), then set `TYPESAFE_API_KEY` or a key file via `[jev].api_key_file`. Without it, recall runs in FTS-fallback mode. The model is set with `[jev].model`: the default `jev-latest` is the official SDK's default and the documentation's example; to pin results, set a version such as `jev-1.13.0`.
- The **`claude` CLI**, installed and logged in, for summaries, labeling and consolidation (`claude -p --model haiku`, isolated — see Security and Troubleshooting). jevmem uses your own Claude login (`claude -p`); the Anthropic API-key variables (`ANTHROPIC_API_KEY`, `ANTHROPIC_AUTH_TOKEN`) are removed from the child environment unless `[llm] claude_use_api_key = true`. These background jobs run unattended every 30 minutes, so check that this kind of automated use fits the terms of your own Claude plan. Optional fallback for summaries and daily consolidation (not labeling): [Ollama](https://ollama.com) with a small local model (`gemma3`). `[llm] backend`/`fallback` and `[index] labeler = "jev"` change this.

## Setup: zero to the full loop

```bash
# 1. install the CLI from a clone, with pipx or into a virtualenv
git clone https://github.com/hojeongna/jev-agent-memory.git
pipx install ./jev-agent-memory
#    or: python3 -m venv ~/.venvs/jevmem && ~/.venvs/jevmem/bin/pip install ./jev-agent-memory
#    (plain `python3 -m pip install .` is refused by Homebrew and Debian/Ubuntu Pythons:
#     "externally-managed-environment", PEP 668)

# 2. in your agent's project directory (where you run `claude --channels …`)
cd ~/my-agent
jevmem init --logs memory            # jevmem.toml + .jevmem/ (DBs, example taxonomy)

# 3. text LLM: defaults are backend = "claude" (Haiku), fallback = "ollama", labeler = "haiku"
claude --version                     # the claude CLI must be installed and logged in
ollama pull gemma3                   # optional: local fallback for summaries + consolidation

# 4. Jev key in a file (works for hooks AND launchd/cron, which ignore your shell profile)
mkdir -p ~/.config/jevmem && printf '%s' "$YOUR_KEY" > ~/.config/jevmem/key && chmod 600 ~/.config/jevmem/key
#    jevmem.toml → [jev] api_key_file = "~/.config/jevmem/key"

# 5. make the taxonomy yours (an LLM can draft it from your logs — docs/taxonomy.md)
$EDITOR .jevmem/taxonomy.json

# 6. hooks: capture (stage 1) + recall (stage 3)
jevmem install-hook                  # merges into .claude/settings.json (backup first, idempotent)
#    --no-capture: recall hook only (also removes jevmem capture hooks already there)

# 7. schedule: digest (stage 2) at :04/:34 + daily consolidation at 04:00
jevmem schedule install              # prints launchd plists / crontab block
jevmem schedule install --write      # registers them now: claude -p runs every 30 min from here on
#    macOS: --no-load writes the plists without loading them

# 8. already have daily logs? label them once (dry-run shows the chunk and call counts first)
jevmem index --dry-run && jevmem index

# 9. check everything
jevmem doctor                        # key, LLM, capture log, hook interpreter, schedule, last runs
```

**The hooks and jobs remember this Python.** `install-hook` and `schedule install` write the absolute path of the interpreter that runs them (`sys.executable`, e.g. pipx's own virtualenv) into the hook commands and the launchd/cron jobs, so they work without your shell's PATH. If that interpreter goes away — virtualenv recreated, `pipx reinstall`, a Python upgrade — run `jevmem install-hook` and `jevmem schedule install --write` again. `jevmem doctor` reports a hook whose interpreter is missing, including hooks that `install-hook --project` wrote into another project.

Then chat with your agent as usual. `jevmem capture tail` shows captured messages; after the next :04/:34 run `memory/<today>.md` has a `## HH:MM summary` section; `.jevmem/inject_log.jsonl` shows what was recalled per message; after 04:00, `jevmem profile list` shows consolidated traits.

Want to try the commands without your own data first? [`examples/`](https://github.com/hojeongna/jev-agent-memory/tree/main/examples) holds invented sample data — daily logs, digest summaries, a captured chat log (`capture.example.jsonl`), a plain chat JSONL and the example taxonomy — which the test suite also uses. In a scratch directory: `jevmem init`, copy `examples/logs/*.md` into `memory/`, then `jevmem index --dry-run` and `jevmem index --no-label` (neither calls an API or `claude`). `jevmem search --fts "nightly backup"` then finds the backup entry, but a one-word query such as `jevmem search --fts "backup"` finds nothing until you set `[fallback] bm25_threshold = 0`: on a corpus this small a single word scores too weakly for the `"auto"` cut-off, which is tuned for real logs. For `jevmem raw` and `jevmem digest --dry-run`, also copy `examples/summaries/*.md` into `memory/` and `examples/capture.example.jsonl` to `.jevmem/chat.jsonl` (the default `[capture].path`), then try `jevmem raw 2026-09-10 09:34`.

Debugging (the example questions refer to the sample data):

```bash
jevmem capture tail                                            # stage 1
jevmem digest --dry-run                                        # stage 2: what would be summarized now, and where (no LLM call)
jevmem search "what did we decide about the export API?"      # stage 3, full pipeline (calls Jev; FTS fallback without a key)
jevmem search --fts "backup"                                   # fallback only, no API call
jevmem raw 2026-09-10 09:34                                    # the chat lines behind a summary
jevmem profile consolidate --date 2026-09-02 --dry-run         # what would be sent; no LLM call, no writes
jevmem index --stats
tail -f .jevmem/inject_log.jsonl .jevmem/logs/*.log            # once the hook and the jobs have run
jevmem install-hook --uninstall ; jevmem schedule uninstall --write
```

Everything is configured in `jevmem.toml` (thresholds, half-lives, caps, schedule times, prompts, instructions sent to Jev, chat sources); **[docs/config-reference.md](https://github.com/hojeongna/jev-agent-memory/blob/main/docs/config-reference.md)** lists every key with its default. Relative paths resolve against the config file's directory. The config is found via `--config`, `$JEVMEM_CONFIG`, `./jevmem.toml`, then `~/.config/jevmem/jevmem.toml`; a file that is only found automatically (the last two) is used only if it belongs to you and isn't writable by group or others. A file named by `--config` or `$JEVMEM_CONFIG` that doesn't exist, isn't valid TOML, or has a wrong value anywhere — a wrong type (`max_picks = "four"`, `log_text = "false"`), a number out of range, a value that isn't one of the choices (`backend = "claude-sonnet"`), a bad template or regex — is an error (exit 2), not a silent fallback to the defaults; the hooks log it instead of failing (see Security), and `jevmem doctor` shows it as FAIL. While the config has an error the recall hook injects nothing, and chat capture goes on only if `[data]` and `[capture]` can still be read (doctor says which), so **run `jevmem doctor` after editing `jevmem.toml` and after upgrading jevmem**. Write regular expressions in single quotes (TOML literal strings, `'^#{1,4}\s*…'`): in double quotes a lone `\` is a TOML error. Unknown sections and keys (a typo such as `[recal]`) are not errors, but `jevmem doctor` lists them. When no config file is found at all, every command except `init` and `doctor` (`search`, `raw`, `profile`, `capture tail`/`log-out`, `digest`, `index`) stops with exit 2 and prints where it looked, and the hooks do nothing (they never write into whatever directory they happen to run in). `--config` works before or after the subcommand: `jevmem --config X profile list` and `jevmem profile list --config X` are the same.

## Labeling limits and outages

- **Per run:** at most `index.cap` chunks (400) are labeled, about 20 per Haiku `claude -p` call. `jevmem index --dry-run` shows the chunk and call counts before anything runs.
- **Relabeling is never automatic:** `--relabel` / `--relabel-topic T` mark chunks stale, old labels stay usable, and the work is spread over capped runs.
- **Unusable answers are capped:** a chunk whose labeling comes back without a valid label `index.max_tries` (5) times — Haiku answering outside the taxonomy, Jev answering non-JSON, an error object or a 4xx other than 402/429 — is given up instead of being retried every 30 minutes; `jevmem index --retry-failed` tries again. Calls that got no answer at all never count (next two points).
- **Outages pause, then retry by themselves.** When the `claude -p` call itself fails — network down, Claude login expired, usage limit reached, timeout — jevmem stops calling it for a while (30 minutes, then 1 h, 2 h, and every 4 h at most; `[llm] claude_pause_minutes` / `claude_pause_max_minutes`) and tries again on its own. These failures never count toward `max_tries`, a success ends the pause, the digest writes every new message as a raw line meanwhile (up to `[digest] raw_fallback_lines` per run; the rest waits for the next run), and `jevmem doctor` shows the pause.
- **Jev outages stop Jev labeling for that run.** A Jev call that times out, can't connect (connection refused, DNS), or gets HTTP 5xx, 429 or 402 counts against no chunk; the chunks stay pending and the next run tries again.
- **Jev labeling** (`labeler = "jev"`, or the Jev fallback for a chunk Haiku answered twice without a valid label) is additionally trimmed to the call budget limits `index.max_cost_usd_per_run` / `index.max_cost_usd_per_day`; `--yes` lifts them.
- **A missing logs folder deletes nothing.** If `[logs] dir` is missing or unreadable (an unmounted disk, a sync folder that is briefly gone, a typo), no file matches `[logs] glob`, or at least 2 and more than half of the indexed log files are gone at once (a folder that came back empty or half synced), indexing keeps every indexed chunk and its labels and reports it (`jevmem doctor` shows `logs-missing`). `jevmem index --force-prune` deletes them on purpose. The digest never re-creates a logs folder that existed: it holds the run (`summary: held`) and the new messages wait in the chat log.

## How the defaults were chosen

The defaults come from the original author's small, informal, non-blind tests on their own (Korean) logs and their own taxonomy. No benchmark figures are published here, and the English example taxonomy shipped with jevmem has **not** been evaluated at all; judge jevmem on your own logs.

- **Recall.** In those tests, the Jev route-and-pick pipeline injected something useful more often, and with less noise, than the keyword-only hook it replaced. Jev's probabilities vary slightly from call to call, so identical settings can pick slightly different chunks.
- **Background models.** Claude Haiku labeled top-level topics about as well as Jev, and its hierarchical labeling (topic first, then a subtopic inside it) keeps every subtopic consistent with its topic, so Haiku labels and writes summaries by default while Jev stays the per-message router and picker, where its speed and probabilities matter. A small local model was slower and weaker (for example at recognizing small talk), so it is only a fallback for summaries and consolidation.

## Fallback behavior

The hook always exits 0 and never prints errors to the conversation. It uses the FTS5 fallback (same output format, header says "FTS5 fallback") when: the episodic DB is missing, the API key is missing, Jev returns an error or times out, total time exceeds `deadline_sec` (4 s, a hard limit even for a server that answers one byte at a time), or the 402 breaker is open (30 minutes after Jev answered HTTP 402; only that status code opens it). The fallback: AND-then-OR trigram match on tokens ≥3 chars, `LIKE` for 2-char tokens (common in Korean) with a document-frequency cap, a bm25 cut-off (`"auto"` scales the reference −10.0 by your corpus size), then time-decay reordering. The profile block is independent: a missing, corrupt or locked profile DB just omits it. If the config file itself can't be loaded, nothing is injected and the reason goes to the hook error log (see Security). Every wait — a Jev call, a locked profile or episodic DB, the fallback's own query — comes out of the same `deadline_sec` and ends 0.3 s before it (room for starting Python, which the deadline doesn't measure); waiting for a locked episodic DB is capped at 3 s.

## Writing a taxonomy

The quality of routing is mostly the quality of your taxonomy. See **[docs/taxonomy.md](https://github.com/hojeongna/jev-agent-memory/blob/main/docs/taxonomy.md)**. Short version:

- ~6–12 top-level topics, ~20–60 subtopics total (hard limit 255), plus one **smalltalk** topic for messages that need no memory.
- Two descriptions per node: **labeling** (long, concrete, with typical nouns and examples from your logs) and **routing** (a few keywords).
- Jev can't write text, so have an LLM (e.g. Claude) draft it from a sample of your logs; then check `jevmem index --stats` topic counts and split crowded subtopics.
- After renaming or restructuring: `jevmem index --relabel --dry-run`, then `jevmem index --relabel`.

## How the profile gets written

Daily consolidation (`jevmem profile consolidate`, scheduled at 04:00) reads yesterday's log — only its first 16,000 characters are sent (`[consolidate].max_log_chars`, configurable); the rest of a longer day is not seen — asks the text LLM for at most 3 candidate traits in strict JSON, and then **verifies every candidate in code** before writing: kind must be one of `preference, pattern, constraint, skill, relationship, goal`; moods/emotions are rejected (they may only be attached to evidence as context); the quote must appear **verbatim** in that day's log; merge/supersede targets must be active traits, and a "new" trait that closely matches an existing one becomes a merge; at most 3 evidence rows per source date *and* per wall-clock day; two candidates of the same run that say the same thing become one trait. An empty log means no LLM call. `--dry-run` shows what would be sent (log size, existing traits, remaining budget) without calling the LLM or writing anything. See **[docs/profile-consolidation.md](https://github.com/hojeongna/jev-agent-memory/blob/main/docs/profile-consolidation.md)** for the prompt and rules.

The write commands it uses are also available by hand (or from your own agent prompt), with **fewer checks**: the kind must be allowed, a merge/supersede target must be an active trait, duplicate evidence is skipped and the daily budget applies; if `--source` is an existing file that doesn't contain `--quote` verbatim, they print a warning but still write. The quote-must-be-in-the-log rule, the mood/emotion filter and turning a near-duplicate "new" into a merge belong to the daily consolidation only.

The evidence date is the `YYYY-MM-DD` in the `--source` file's name (`memory/2026-09-02.md` → 2026-09-02), never one from a folder above it; without a date in the name it is today. The budget has two caps, and a refusal names the one that was hit: 3 pieces of evidence per cited date, and 3 written per wall-clock day whatever dates they cite. Adding `--backfill` (to `new`, `merge`, `supersede` or `consolidate`) skips the per-day total so old logs can be processed; the per-date cap still applies.

```bash
jevmem profile candidates "Prefers short bullet summaries"   # similar existing traits (JSON), no decision
jevmem profile merge 12 --source memory/2026-09-02.md --quote "short bullet summaries over long paragraphs"           # dry-run
jevmem profile merge 12 --source memory/2026-09-02.md --quote "short bullet summaries over long paragraphs" --commit
jevmem profile new --kind preference --statement "..." --source ... --quote "..." --commit
jevmem profile supersede 12 --statement "..." --source ... --quote "..." --commit   # old kept for audit
jevmem profile new --kind goal --statement "..." --source memory/2026-08-15.md --quote "..." --commit --backfill
jevmem profile list
jevmem profile show 12
jevmem profile delete 12 --commit
jevmem profile stats
jevmem profile inject --preview --query "..."
```

## Limits

- **Fixed taxonomy.** A brand-new interest lands in the nearest topic; rare-word matching helps for a while. The risky case is Jev routing an unfamiliar topic to *smalltalk*, which injects nothing. Review the log for smalltalk-routed or low-confidence messages and extend the taxonomy.
- **Label lag.** Conversation since the last digest run isn't recallable yet (usually fine: the agent still has it in context).
- **Latency.** Adds ~1 s per message on the Jev path. Background Haiku calls take several seconds each but never block the conversation.
- **Profile matching is lexical.** Word overlap decides which traits are "related"; a shared common word can attach an irrelevant trait.
- **Tokenizer.** Simple rules for English and Korean (particle stripping); other languages pass through as words. FTS trigram can't match 2-character tokens (handled with `LIKE`).
- **Summary quality depends on your text LLM.** Small local models can miss details or paraphrase loosely; the prompt forbids invention and spells out speaker/status/number rules, but nothing verifies digest bullets automatically (consolidation quotes *are* verified against the log). The raw pointer lets the agent check a summary against the chat; see [docs/summary-accuracy.md](https://github.com/hojeongna/jev-agent-memory/blob/main/docs/summary-accuracy.md) for the kinds of errors seen and what was changed.
- **Raw lines exist only since capture started.** Summaries older than the capture log (or written by hand) get no pointer.
- **Only chat-app conversation is captured by default.** Work you do with the agent in the terminal isn't summarized unless you switch both `[digest].source` and `[context].source` to `claude_transcript`.
- **A missed daily consolidation is not caught up.** If `claude -p` is paused at 04:00 (an outage, see above), that day's consolidation reports `llm-unavailable`; run `jevmem profile consolidate --date YYYY-MM-DD` later for it.
- **Privacy:** see [What leaves your machine](#privacy-what-leaves-your-machine).

## Privacy: what leaves your machine

jevmem keeps its data on your machine (`.jevmem/` and your daily logs), but four kinds of text are sent out:

1. **To Anthropic, through `claude -p` (summaries, daily consolidation, labeling).** The digest sends the chat lines since its last run (nearly whole messages), in calls of up to 24,000 characters each: a longer stretch goes out in several calls, all of it. Daily consolidation sends the first 16,000 characters of a day's log plus your existing traits. Labeling sends log chunks, about 20 per call (each cut at 900 characters).
2. **To Jev (TypeSafe), as a labeling fallback.** When Haiku has twice answered without a valid label for a chunk (`[index] haiku_max_failures`) and a Jev key is configured, that chunk's text (up to 1,500 characters) is sent to Jev, together with the log file's path relative to your logs folder (a `[file] 2026-09-10.md` line). With `[index] labeler = "jev"`, every chunk goes to Jev that way.
3. **To Jev (TypeSafe), for recall on every message.** The hook sends the current message, the last 4 chat lines (`[context] lines`), today's date (`[recall] include_today`) and about 100 dated 200-character excerpts of your logs.
4. **Other people's messages in group chats.** If the agent sits in a group chat, other members' messages and display names are captured (`.jevmem/chat.jsonl`), written into summaries (`User (name)`) and sent along in 1–3. Telling them and getting their consent is your responsibility.

How to turn things off:

- `[hook] log_text = false` — the local recall log (`.jevmem/inject_log.jsonl`) stops storing message text.
- `[recall] include_today = false` — no date line in what goes to Jev.
- `[context] source = "none"` — no recent chat lines go to Jev, only the current message.
- `[llm] backend = "ollama"` (or `"none"`) with `fallback = "none"` — summaries and consolidation never go to Anthropic (`"none"`: raw lines, no consolidation). Labeling still uses `claude -p` unless `[index] labeler = "jev"`.
- No Jev key — nothing goes to Jev: recall uses the local FTS fallback and labeling has no Jev fallback.
- `[digest] name_speakers = false` — summaries don't label lines with sender names (the chat log still stores them).
- Removing jevmem: `jevmem install-hook --uninstall` and `jevmem schedule uninstall --write` stop all capturing and sending. Delete `.jevmem/` to remove the captured chat, the recall log and the databases; the digest's `## HH:MM summary` sections stay in your daily logs until you delete them.

## Security

- **Local data is private by default.** Files jevmem creates — `jevmem.toml` and `.jevmem/taxonomy.json` written by `jevmem init`, `.jevmem/chat.jsonl`, `inject_log.jsonl`, `capture_seen.txt`, the SQLite databases, state, lock and pause files, and new daily logs written by the digest — get mode 0600, and directories it creates (including `.jevmem/logs/`) get 0700; so do `.jevmem/hook_settings.json`, a `.claude/settings.json` that `install-hook` creates, the settings backups it makes and the launchd plists `schedule install --write` writes. Files are rewritten through a private temporary file (0600, created exclusively) and flushed to disk before the rename. The scheduler's `.log` files are created by launchd/cron with their own defaults, inside that directory. Existing files and directories are never chmod-ed (rewriting `.claude/settings.json` keeps its mode, and a settings file that is a symlink stays a symlink), so check projects set up with an older version yourself.
- **The Jev key** comes from `$TYPESAFE_API_KEY` or a key file (keep it `chmod 600`; `jevmem doctor` warns when others can read it); the Jev client masks it in every error message. `[jev] url` must be `https://` (plain `http://` only for `127.0.0.1`/`localhost`), without a `user@` part or a backslash, since the key goes with every request; a redirect from the server is not followed (the key would go along) but reported as an error.
- **Config files are trusted only when named or yours.** A `jevmem.toml` found automatically in the current directory (or `~/.config/jevmem/`) is used only if it belongs to you and isn't writable by group or others — like git's `safe.directory` — because it decides which program runs as `claude` and where the Jev key is sent. `[llm] claude_bin` must be a command name or an absolute path (a relative `./claude` is refused). The hooks and scheduled jobs always pass their config with `--config`.
- **Background `claude -p` children** run from an empty temp directory, using your own `claude` login (the Anthropic API-key variables are removed unless `[llm] claude_use_api_key = true`), with no MCP servers or plugins, jevmem's hooks disabled, the Jev key variable removed, the prompt on stdin (not on the command line, UTF-8 whatever the locale), and **no tools**: the conversation they summarize is untrusted text (anyone in a group chat can write it). jevmem passes `--tools ""` (every built-in tool off) when `claude --help` lists that option (`[llm] claude_tools_off = "auto"`; `true`/`false` force it), and always `--disallowedTools` with `[llm] claude_disallowed_tools` (default `Bash,Edit,Write,NotebookEdit,WebFetch,WebSearch`). If `--tools` isn't available or the help can't be read, the deny list also gets `Read`, `Glob`, `Grep`, `Task` and the other built-in tools that read files, search or start agents. The setting is a comma-separated string (split on commas only, so `Bash(rm -rf *)` stays one entry) or a TOML array; `""` leaves the `--disallowedTools` flag out (only if your `claude` rejects it; with `claude_tools_off = false` as well, `jevmem doctor` warns). A call that times out is stopped together with every process it started.
- **Recalled memory is quoted, not obeyed — but it can carry text someone planted.** Anything written in a captured chat (in a group chat, by anyone in it) can end up in a summary bullet or a profile trait (a trait's quote is only checked to be in the log) and is then injected into later prompts. jevmem marks every injected block with one line saying it is past records, not instructions (`[hook] inject_note`), and the background `claude -p` children have no tools. If the agent runs with broad permissions, also consider: no capture in group chats you don't control, `jevmem profile list` / `jevmem profile delete` to review traits, and `jevmem raw` to check a summary against the original lines.
- **Hooks never block a message.** The hook entries always exit 0 and print nothing on errors — including bad arguments, a missing `--config` file and no config file at all (exit code 2 from a `UserPromptSubmit` hook would block the prompt). The reason goes to `~/.local/state/jevmem/hook-errors.log` (`$XDG_STATE_HOME` respected), and `jevmem doctor` shows the last entry (delete the file to clear it). A wrong value in the config, or a TOML syntax error outside `[data]`/`[capture]`, stops recall, but chat capture goes on with only `[data]` and `[capture]` read from the file (logging a line per message); if those two can't be read, capture stops too. Error lines in that log, in `inject_log.jsonl` and in `status.json` keep the error's class and a short fixed reason only, never message text or a server's response body. `jevmem hook --help` (or running a hook entry with nothing piped in) prints the help instead.
- **What leaves your machine:** see [Privacy](#privacy-what-leaves-your-machine).

## Troubleshooting

**Channel plugin disconnects (Telegram/Discord) right after a digest or consolidation run.**
A child `claude -p` that loads your user-scope plugins starts its own copy of the channel
plugin, which competes for the bot's message polling and knocks out the main session's
plugin. jevmem's `claude` backend prevents this by always running
`claude -p --model haiku --tools "" --disallowedTools … --strict-mcp-config --mcp-config <tmp>/empty.json`
(`{"mcpServers":{}}`, the prompt on stdin) from an empty temp directory with `JEVMEM_DISABLE_HOOK=1`.
If you call `claude -p` from your own scripts on the same machine, use the same flags. `--bare`
is not used because in that mode `claude` reads only an API key and never the OAuth login.
Using `backend = "ollama"` and `labeler = "jev"` avoids `claude -p` entirely.

**Summaries come out as "(raw)" / labeling stopped.** `jevmem doctor` shows
`claude -p: paused until HH:MM after N failed calls (reason)` when the `claude -p` call itself
failed (network, `login`, `usage limit`). jevmem waits and retries by itself (30 min up to 4 h
between attempts). Meanwhile no chunk's labeling attempt is counted, and the digest writes
every new message as a raw line (`## HH:MM summary (raw)`) instead of a summary — at most
`[digest] raw_fallback_lines` (200) per run; messages beyond that stay in the chat log and the
next run continues from the first one not written, so no user message is skipped. Only
assistant/script messages after the last user message that don't fit are counted in one line
instead of written out (the chat log keeps them for `jevmem raw`). Raw sections are not
re-summarized later. If the reason is `login`, run `claude` once and log in again.

**"digest_state.json was unreadable" / "continuing from the read position saved in …" /
"resuming with chat records from …".** The file that remembers how far the digest has read was
empty or damaged (for example after a power loss). It is moved aside (`digest_state.json.bak-…`)
and the digest continues exactly where the last completed run stopped, from the copy it writes
with every run (`digest_state.backup.json`). Without that copy it continues with the chat records
after the newest summary heading in your daily logs instead of summarizing the whole chat log
again; records without a readable time can't be placed that way and are skipped (the warning
says how many).

**Labels kept although the logs folder is gone ("logs-missing", "summary: held").** `jevmem
doctor` / the digest output show it when `[logs] dir` doesn't exist, can't be read, no file
matches `[logs] glob`, or more than half of the indexed log files are gone at once. Nothing is
deleted, and the digest writes nothing (the new messages wait in the chat log); fix the path or
bring the folder back. To really remove the indexed chunks of files that are gone, run `jevmem
index --force-prune`; if you removed the whole folder on purpose, create it again (`mkdir`).

**`FAIL config` after editing `jevmem.toml` or upgrading.** While the config has an error, the
recall hook injects nothing; `jevmem doctor` also says whether chat capture goes on (with only
`[data]` and `[capture]`) or has stopped. Fix the value it names (regular expressions go in
single quotes; the configuration reference linked above shows every default in a form you can
copy) and run `jevmem doctor` again.

**"file is not a database" / "database disk image is malformed".** `jevmem doctor` names the
damaged file. The episodic index (`.jevmem/episodic.sqlite`) is rebuilt from your daily logs:
move it aside (`mv .jevmem/episodic.sqlite .jevmem/episodic.sqlite.broken`) and run `jevmem
index` (every chunk is labeled again). The profile (`.jevmem/profile.sqlite`) can't be rebuilt
from the logs: restore it from a backup (copy it now and then), or move it aside to start an
empty profile.

**Nothing is captured / recall stopped.** `jevmem capture tail` is empty → check that `jevmem install-hook`
ran in the project where you start `claude --channels …`, and that your plugin's
`<channel source="…">` and reply tool are listed in `[capture]`. Then run `jevmem doctor`: it
shows hook errors (a moved config file, bad arguments, a config error) and a hook whose Python
is missing. The hook error log is shared by all projects; an entry older than a day is shown as
info, and deleting the file clears it.

**Hooks or scheduled jobs stopped after recreating the virtualenv.** They run the Python
that was current at install time. Run `jevmem install-hook` and `jevmem schedule install --write` again.

**No summaries.** `jevmem doctor` shows the last digest run; `.jevmem/logs/digest.log` has
the scheduler output. launchd/cron don't read your shell profile: use `[jev].api_key_file`
and make sure `ollama`/`claude` are on the PATH written into the schedule. With
`source = "jsonl"`, check `[digest] jsonl_path`: the digest reports an unset or missing file.

**Chunks stay unlabeled.** `jevmem doctor` / `jevmem index --stats` show chunks given up after
`index.max_tries` unusable answers (for example a taxonomy the model keeps answering outside
of). Fix the labeler or the taxonomy, then run `jevmem index --retry-failed`. `doctor` also
warns when the index has chunks but none is labeled yet: Jev recall needs labels, so run
`jevmem index`.

**Recall always says "FTS5 fallback".** See the `error` field in `.jevmem/inject_log.jsonl`
(missing key, no labeled chunks yet, timeout, or the 30-minute breaker after an HTTP 402).

## Development

```bash
python3 -m unittest discover -s tests     # fake Jev client, fake LLM backends, local test servers only: no calls to Jev or Anthropic
```

Package name `jev-agent-memory`, import/CLI name `jevmem`. Layout (`src/jevmem/`): stage 1 `capture`; stage 2 `digest`, `llm` (text backends), `chunking`, `labeler`, `indexer`, `store`, `taxonomy`; stage 3 `hook`, `recall`, `raw`, `text`, `jev_client`; profile memory `profile`, `consolidate`; setup and operations `cli`, `config`, `install`, `schedule`, `status`, `fsutil` (private files, hook error log).

## License

MIT — see [LICENSE](https://github.com/hojeongna/jev-agent-memory/blob/main/LICENSE).
