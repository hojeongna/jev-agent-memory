# Stage 1 — Capture, Stage 2 — Digest

## Stage 1: capture (`jevmem capture`)

Only the **chat-channel conversation** is saved: what the user typed in Telegram/Discord
(through Claude Code's channel plugins) and what the agent actually sent back to that
chat. Claude Code's internal thinking, tool calls, and replies that never reached the chat
are not saved — they aren't part of the conversation the user experienced, and they would
flood the summaries.

`jevmem install-hook` installs two capture hooks next to the recall hook:

| hook | command | what it records |
|---|---|---|
| `UserPromptSubmit` | `jevmem capture inbound` | each `<channel source="plugin:telegram:telegram" chat_id=… message_id=… user=… ts=…>text</channel>` (and `plugin:discord:discord`) block in the prompt; deduplicated by channel + chat + message_id |
| `PostToolUse`, matcher `mcp__plugin_telegram_telegram__reply\|mcp__plugin_discord_discord__reply` | `jevmem capture outbound` | `tool_input.text` and `chat_id` of the reply; the sent message id from the tool response when present |

Scripts that message the user outside the plugin (cron alerts, notifications) can record
what they sent so the digest sees it too:

```bash
jevmem capture log-out "Disk usage is at 80%" --channel telegram --chat-id 123
jevmem capture tail            # last 20 records, labeled like the digest and `jevmem raw` see them
```

`capture tail` labels each record `User (name)`, `Assistant`, or `Assistant (script)` for a
`log-out` record — the same labels the summarizer and `jevmem raw` use.

Record format (`.jevmem/chat.jsonl`, configurable as `[capture].path`):

```json
{"ts": "2026-09-10T09:05:00+09:00", "dir": "in", "channel": "telegram", "chat_id": "111",
 "message_id": "10", "user": "someone", "text": "Why did the backup fail?"}
```

Other chat plugins: add their `<channel source="…">` value to `[capture.sources]` and their
reply tool to `[capture.reply_tools]`, then re-run `jevmem install-hook`. All capture hook
entries exit 0 silently whatever happens (even with bad arguments, a missing `--config`
file, or no config file found at all — then nothing is recorded anywhere); problems are
written to the per-user hook error log (`$XDG_STATE_HOME/jevmem/hook-errors.log`, default
`~/.local/state/jevmem/hook-errors.log`; the error's class and a short reason only, no message
text) and shown by `jevmem doctor`. `jevmem capture inbound --help` prints the help.
`jevmem install-hook --no-capture` installs only the recall hook and removes jevmem capture
hooks already in that settings file.

**Several messages in one prompt.** When messages arrive while the agent is busy, one prompt
can carry several `<channel>` blocks. Capture writes one record per block (each deduplicated
by its own message_id). Recall treats them as one message: Jev's state gets all of them,
joined in the order they appear; the keyword tokens (rare words, FTS fallback, profile
matching) are taken newest message first; the blocks are not repeated as "recent
conversation" lines. Only the first 262,144 characters of a prompt are searched for blocks,
with a plain linear scan, so even a huge prompt full of unclosed `<channel` tags is handled in
milliseconds. A block starts only at `<channel` followed by a space or `>`, so a tag such as
`<channel-note>` is not mistaken for one.

**Message ids of replies.** The sent message's id is read from the reply tool's response:
an explicit `message_id`/`messageId` wins, otherwise a bare `id` (`"sent (id: 853)"`,
`{"id": 5}`); `chat_id`, `user_id` and similar are never taken for it.

**File modes.** New files under the data directory (`chat.jsonl`, `capture_seen.txt`,
`inject_log.jsonl`, state files, databases) and new daily logs are created 0600, new
directories 0700; existing files keep their permissions.

**Duplicates.** The duplicate check (`capture_seen.txt`) runs under a file lock with an
atomic rewrite, so two hook processes handling the same prompt at once can't both record
it. A message id is marked as seen only after its record was written: if writing fails
(disk full), the same message is saved when the prompt comes again instead of being
skipped forever. The lock wait is capped at 2 seconds for all blocks of one prompt
together; if another process holds it longer, the messages are saved without the check (a
rare duplicate line rather than a lost message) and the hook error log says so. A lone
surrogate character in a message (a broken emoji) is saved as `?` rather than losing the
message.

The same log feeds the recall hook's "recent conversation" lines (`[context].source =
"capture"`, the default).

## Stage 2: digest (`jevmem digest`, every 30 min)

One run, scheduled at :04 and :34:

1. Read the records appended to the chat log since the last run (byte offset in
   `.jevmem/digest_state.json`; an incomplete last line waits for the next run). Every run
   also writes the same offsets to `.jevmem/digest_state.backup.json`. If the state file is
   empty or damaged (say, after a power loss), it is moved aside
   (`digest_state.json.bak-YYYYmmdd-HHMMSS`) and the run continues exactly where the last
   completed run stopped, from the backup copy. Without a usable copy, it continues with the
   records after the newest summary heading in the daily logs (or from the start of today if
   there is none); a record without a readable time (the `ts` of a `jsonl` source is
   optional) can't be placed that way, so such records before that point are skipped, not
   summarized a second time, and the run says how many. The same happens when the state file
   is missing while the daily logs already have summaries (with the `claude_transcript`
   source a missing state file and no copy still means a first run, which only records a
   baseline). The chat log is never summarized again from the beginning. Both files are
   written atomically and flushed to disk.
2. **No new user message → no summary and no LLM call** (the labeling step still runs, so
   manual log edits get indexed).
3. Otherwise summarize with the text LLM (`[llm].backend`) into **one bullet per topic**
   (at most 5, 1–2 sentences each) and append to `<logs>/YYYY-MM-DD.md`:

   ```markdown
   ## 09:34 summary
   - Nightly backup failed because the cron entry used a relative path; switched to an absolute path, to be checked after tomorrow's run.
   - Friday dinner: table for six booked at 7 pm; cake still to be ordered.
   ```

   Why one topic per bullet: the chunker keeps **each bullet of a `## HH:MM summary`
   section as its own chunk** (`[index].one_chunk_per_bullet_heading`), so every chunk gets
   a clean single-topic label. In the original deployment, summaries written as one bullet
   mixing several topics left a noticeable share of chunks with ambiguous labels. Other
   markdown sections keep the normal packing (bullets merged up to 450 chars).

   If the LLM is unavailable or returns no usable JSON (or `backend = "none"`), every new
   message is written as a raw line under `## 09:34 summary (raw)` — at most
   `[digest] raw_fallback_lines` (200) per run. Messages beyond that stay in the chat log: the
   offsets advance only past the lines actually written, and the next run continues with the
   first message not written (headed with the time of the last message written, so `jevmem
   raw`'s windows still line up). A tail without any user message is consumed right away
   (the next run would otherwise skip it as "no new user message"), within the same limit:
   what doesn't fit becomes one line counting those assistant/script messages; the chat log
   keeps them for `jevmem raw`. Raw sections are not re-summarized later.

   A window longer than `[digest] max_chars` (24,000) is summarized in several parts, one
   call each, each part headed with the time of its last message; no message is cut off.

   **Midnight.** New records are grouped by their local date. Chat from an earlier day — the
   00:04 run summarizing 23:40–23:55, or a catch-up after the machine was off — goes to
   *that* day's file, headed with the time of its last message (`## 23:55 summary`), so
   `jevmem raw`'s windows still line up. Records of a day without a user message join the
   neighbouring day's section.
4. **In the same run**, chunk the logs and label new/changed chunks (the same work as
   `jevmem index`). If labeling fails, the summary is kept and the unlabeled chunks are
   retried next run — up to `[index].max_tries` (5) unusable answers per chunk, after which
   the chunk is given up (reported as `gave_up`) until `jevmem index --retry-failed`.

Only one digest runs at a time (`.jevmem/digest.lock`): one that starts while another is
still running exits at once, so overlapping scheduled runs can't summarize the same lines
twice. A new daily log file is created with its header and first section in one
append-mode write, so nothing another writer appended is overwritten.

**The logs folder is created only once.** The first summary creates `[logs] dir` if it
doesn't exist yet. After that (the folder existed before, or the index holds chunks from
it) a missing folder holds the run: `summary: held, logs dir … does not exist`, no LLM call,
nothing written, and the new messages stay in the chat log for the next run. A sync folder
that is briefly gone or an unmounted disk then can't come back as a fresh folder with one
new file, which the labeling step would read as "every other log was deleted". If you
removed the folder on purpose, create it again (`mkdir`).

### Labeling (`[index].labeler`)

- `haiku` (default): ~20 chunks per isolated `claude -p --model haiku` call. Hierarchical:
  the model picks one top-level topic, then one subtopic **belonging to that topic**, and a
  second (topic, subtopic) only if the chunk is genuinely mixed; strict JSON keyed by chunk
  id. The repeated `## HH:MM summary` heading is stripped before sending. Stored as
  p = 0.9 (first) and 0.3 (second) so recall's thresholds work unchanged; each chunk
  records its labeler. Invalid/missing entries fail only that chunk; a chunk Haiku answered
  for twice without a valid label (`haiku_max_failures`) is labeled by Jev if a Jev key is
  configured, otherwise Haiku tries again on later runs. There is no Ollama fallback for
  labeling.
- `jev`: one Jev call per chunk (top-level + flat subtopic with probabilities), trimmed to
  the call budget limits `max_cost_usd_per_run` / `max_cost_usd_per_day`.

Either way, every attempt whose answer came back unusable counts toward `[index].max_tries`
(default 5): Haiku answering without a valid label for the chunk, or Jev answering non-JSON,
an error object, or an HTTP 4xx other than 402 and 429. A chunk that reaches it is given up:
it stays unlabeled (a stale chunk keeps its old label), `jevmem index` and the digest report
it as `gave_up`, `jevmem doctor` and `jevmem index --stats` count it, and no further
`claude -p` or Jev call is made for it until `jevmem index --retry-failed`. A call that got no
answer at all never counts (next paragraph).

**Outages are not the chunks' fault.** When the `claude -p` call itself fails — network
down, Claude login expired, usage limit reached, timeout, `claude` missing — no
chunk's attempt is counted. Instead jevmem pauses `claude -p` (30 minutes after the first
failure, then 1 h, 2 h, and every 4 h at most; `[llm] claude_pause_minutes` /
`claude_pause_max_minutes`), skips Haiku labeling and Haiku summaries while paused (the
digest falls back to Ollama, then raw lines), and tries again by itself. A successful call
ends the pause. The state lives in `.jevmem/claude_breaker.json` (0600); `jevmem doctor`
shows it. A pause never lasts longer than `claude_pause_max_minutes`, even if that file is
damaged or the clock was set wrong. Jev likewise: a call that times out, can't connect
(connection refused or reset, DNS), or gets HTTP 5xx, 429 or 402 stops this run's Jev
labeling without counting against any chunk; the chunks stay pending for the next run.

**A missing logs folder deletes nothing.** Chunks whose log text disappeared are normally
deleted from the index. But if `[logs] dir` doesn't exist, can't be read, or no file
matches `[logs] glob` (an unmounted disk, a sync folder that is briefly gone, a typo) while
the index has chunks, the run keeps all of them and their labels, reports `logs_held`
(`jevmem doctor`: `logs-missing`), and labels nothing new from the logs. `jevmem index
--force-prune` deletes them on purpose.

`jevmem index --dry-run` shows the chunk and call counts; relabeling only happens with
`--relabel` / `--relabel-topic`.

```bash
jevmem digest --dry-run     # how many new messages would be summarized, into which file;
                            # never calls the LLM, writes and labels nothing
jevmem digest
jevmem digest --no-index    # summary only
```

`jevmem index` remains for manual runs and backfilling existing logs
(`jevmem index --dry-run` first).

### Sources (`[digest].source`)

- `capture` (default): the Stage 1 chat log.
- `jsonl`: any chat JSONL (`{"role": "user"|"assistant"}` or `{"dir": "in"|"out"}`, text in
  `text`/`content`, optional `ts`), named by `[digest] jsonl_path` (and `[context] jsonl_path`
  for the recent lines recall sends). If `jsonl_path` is unset or the file doesn't exist,
  the digest says so (`no-source`) instead of reporting "no new user messages", and `jevmem
  doctor` marks it.
- `claude_transcript` (fallback, off by default): Claude Code's session files in
  `~/.claude/projects/<slug>/*.jsonl` (slug = absolute project path with non-alphanumerics
  replaced by `-`). Keeps user/assistant text, drops tool calls/results, thinking,
  subagents and command records; the first run only records a baseline. Use this only if
  you talk to the agent in the terminal rather than through a chat app.

### Text LLM (`[llm].backend`, then `[llm].fallback`)

Default: `backend = "claude"` (Haiku), `fallback = "ollama"`; if both fail the raw lines
are written.

- `ollama`: local, `ollama pull gemma3` (or set `ollama_model`).
- `claude`: uses your own `claude` login (`ANTHROPIC_API_KEY` and `ANTHROPIC_AUTH_TOKEN` are
  removed from the child's environment unless `[llm] claude_use_api_key = true`), from an
  empty temporary directory with `JEVMEM_DISABLE_HOOK=1`, the prompt on **stdin**:

  ```
  claude -p --model haiku --tools "" --disallowedTools Bash,Edit,Write,NotebookEdit,WebFetch,WebSearch \
         --strict-mcp-config --mcp-config <tmp>/empty.json
  ```

  where `empty.json` is `{"mcpServers":{}}`. The empty directory keeps your project's
  CLAUDE.md and hooks out; the env var turns jevmem's own hooks into no-ops; the strict,
  empty MCP config starts **no plugins** in the child. Without it the child loads your
  user-scope plugins, including a Telegram/Discord channel plugin, which competes for the
  bot's message polling and disconnects the main session's plugin. The chat being summarized
  is untrusted text, so the child gets **no tools**: `--tools ""` turns every built-in tool
  off. Whether your `claude` has that option is read from `claude --help` (no model call,
  once per binary; `[llm] claude_tools_off = "auto"`, or `true`/`false` to force it). Without
  it, `--disallowedTools` also lists `Read`, `Glob`, `Grep`, `Task` and the other built-in
  tools that read files, search or start agents. `--disallowedTools` itself comes from
  `[llm].claude_disallowed_tools` (`""` leaves it out): a string is split on commas only and
  passed as one comma-separated argument (so `Bash(rm -rf *)` keeps its spaces), a TOML
  array as one argument per entry. Another flag always follows a variadic list. The prompt
  goes through stdin, so it never shows up in `ps` and is not limited by the size of a
  command-line argument. `--bare` is not used because in that mode `claude` reads only an API
  key, never the OAuth login. Labeling and daily consolidation use the same isolation;
  `jevmem doctor`'s check (`claude --version`, no prompt) uses the same directory, env var and
  MCP flags. It does not check whether `claude` is logged in.
- `none`: raw lines only.

### Default prompt (`[digest].prompt`)

Placeholders `{max_topics}`, `{language}`, `{conversation}`, `{user_label}`,
`{assistant_label}`, `{script_label}`, `{truncation_marker}`; literal braces doubled. The
text below writes those literal braces single, for reading: pasted into `jevmem.toml`, its
`{"topics": ...}` line becomes `{{"topics": ["...", "..."]}}`. A custom prompt may leave any
of the placeholders out. The rules come from the original deployment's
summary-accuracy work: see **[summary-accuracy.md](summary-accuracy.md)**.

```
Below is a chat conversation between a user and their AI assistant from the last few minutes. Summarize it for the assistant's daily log, written in {language}.
Each line is "[HH:MM] Speaker: text". Speakers:
- {user_label}: the user. "{user_label} (name)" means several people are talking; keep each person separate.
- {assistant_label}: the assistant's own replies.
- {script_label}: a message a script sent (alert, reminder, relayed news). What it reports is not something the user or the assistant said or did; write it as "an alert reported ...".
Rules:
- One item per distinct topic, at most {max_topics} items. Never put two topics in one item. If there are more topics than that, group related ones so that no topic is left out; topics where the user asked for or decided something come first.
- Each item is 1-2 COMPLETE sentences saying who did/asked/decided what and with what result, including the concrete numbers, names, dates and file or product names mentioned. No title-style fragments (not "Backup issue" but "The user reported that the nightly backup failed; the assistant found a relative path in the cron entry and changed it to an absolute path.").
- Speakers: say who said or did each thing exactly as the line labels show. Never credit the user's idea to the assistant, or the assistant's proposal to the user.
- Keep the status exact: proposed / agreed to do / in progress / done / open question. Something only proposed, planned or decided-to-do must not be written as done. The assistant saying "I'll do it" is planned, not done. A user line ending in a question or "maybe we should ..." is a question or proposal, not a decision. A question nobody answered is not an agreement. Keep negations ("not", "don't", "without") as they are.
- Numbers, names, IDs and dates: copy them exactly as written, in the same notation ("1,200" stays "1,200", "three" stays "three"), and never swap which number belongs to which thing. The numbers in one sentence come from the line they were said in; attach a condition ("after the release") only to the thing it was said about.
- A line ending in "{truncation_marker}" was cut off; do not guess the rest.
- Only facts stated in the conversation; do not guess, generalize or add advice. Skip greetings and small talk.
Mistakes to avoid (invented examples, not from this conversation):
- "[10:02] {assistant_label}: I'll rerun the import tonight." Wrong: "The assistant reran the import." Right: "The assistant said it would rerun the import tonight."
- "[11:15] {script_label}: PR #412 was merged." Wrong: "The assistant merged PR #412." Right: "An alert reported that PR #412 was merged."
- "[14:40] {user_label}: should we move the demo to Friday?" Wrong: "They agreed to move the demo to Friday." Right: "The user asked whether to move the demo to Friday."
- "[15:03] {user_label} (Dana): the budget is 1,200" / "[15:04] {user_label} (Lee): no, 1,500 for the venue". Wrong: "The budget is 1,500." Right: "Dana said the budget is 1,200; Lee said 1,500 for the venue."
Before answering, check every item against the lines it came from: speaker, status, numbers.
Answer ONLY with JSON: {"topics": ["...", "..."]}

<conversation>
{conversation}
</conversation>
```

Each item becomes one `- ` bullet. `{"bullets": [...]}` and the legacy
`{"summary": "..."}` (written as a single bullet) are also accepted.

### What the summarizer sees

The conversation is passed as `[HH:MM] Speaker: text` lines:

| label | who |
|---|---|
| `User` | a user message; `User (name)` when more than one sender name appears in the batch (group chats; `[digest].name_speakers`) |
| `Assistant` | a reply the agent sent through the chat plugin |
| `Assistant (script)` | a message recorded with `jevmem capture log-out` (alerts, reminders, relayed news; stored with `"via": "script"`); label in `[context].script_label` |

Messages are sent (nearly) whole: each line is capped at `max_line_chars` (default 1,500)
and a cut line ends in ` …(truncated)` (`truncation_marker`), which the prompt says not to
guess past. A window over `max_chars` (default 24,000) is split into several calls instead of
dropping lines. Cutting lines at 300 characters was one of the main sources of invented
details in the original deployment.

## Raw pointer: from a summary back to the chat (`jevmem raw`)

A summary bullet is an index entry, not the source of truth. Recall therefore adds one line
under every injected chunk that comes from a `## HH:MM summary` section:

```
[2026-09-10]
## 09:34 summary
- The assistant switched the nightly backup cron entry to an absolute path and said it would check tomorrow's run; the check has not happened yet.
  ↳ raw: jevmem raw 2026-09-10 09:34
```

Only the pointer is injected, never the raw text, so the per-message context stays small.
When a detail matters (who agreed, which number), the agent runs the command:

```bash
jevmem raw 2026-09-10 09:34              # the chat lines that summary was written from
jevmem raw 2026-09-10 09:34 budget 1,500 # only lines containing a keyword, ±1 line of context
jevmem raw 2026-09-10 --around 09:20     # the summary window that contains 09:20
jevmem raw 2026-09-10                    # the whole day
jevmem raw 2026-09-10 --max 40           # at most 40 lines
```

```
[2026-09-10 09:34 raw] window 09:05~09:34, 3 lines · keywords ['1,500']: 1 matching line (±1 line of context)
  [09:20] User (dana): team dinner budget is 1,200
» [09:22] User (lee): no, 1,500 for the venue
```

Window rule: the summary headed `HH:MM` covers (the previous summary heading of that day,
`HH:MM`]; the first summary of a day starts at the last summary heading of the previous
day's log, or at midnight. A heading with an explicit range (`## 09:00~09:34 summary`) uses
that range. The lines are read from the digest source (the capture log by default), and the
pointer is only shown when that source reaches back to the chunk's date; for older dates
`jevmem raw` says the raw lines are not available (and whether a daily log exists for that
date). A date or time that doesn't exist (`2026-02-30`, `9:99`) is an error (exit 2).

To try it on the invented sample data, copy `examples/summaries/*.md` into your logs
directory and `examples/capture.example.jsonl` to `.jevmem/chat.jsonl` (the default
`[capture].path`), then run `jevmem raw 2026-09-10 09:34`.

Settings:

| key | default | meaning |
|---|---|---|
| `[recall].raw_pointer` | `true` | add the pointer line |
| `[recall].raw_pointer_command` | `"jevmem raw"` | e.g. `"jevmem --config /path/jevmem.toml raw"` if the agent runs from another directory |
| `[recall].raw_pointer_format` | `"  ↳ raw: {command} {date} {time}"` | the line itself |
| `[raw].heading_re` | matches `## HH:MM summary` and `## HH:MM~HH:MM summary` | change it together with `[digest].title` |
| `[raw].skip_prefixes` | `[]` | hide status-noise messages from `jevmem raw` |

## Whole bullet or whole section in recall (`[recall].expand`)

Recall picks chunks (one bullet per chunk for summary sections, ≤450 characters for other
markdown). `[recall].expand` chooses what is injected for each pick:

| value | injected | when |
|---|---|---|
| `"none"` (default) | the chunk, cut at `[hook].max_chunk_print` (400) | smallest context |
| `"bullet"` | the whole bullet(s) the chunk came from, re-read from the log (undoes the 900-character hard cut and the 400-character print cut) | long bullets |
| `"section"` | the whole section: heading plus every bullet (e.g. the full `## 09:34 summary`) | neighbouring topics of the same half hour help |

Budget: at most `max_picks` (4) items as before, each cut at `expand_max_chars` (1,200), and
all items together within `max_inject_chars` (4,000; the first item is always kept). Two
picks that expand to the same section are injected once. If the log was edited after
labeling and the chunk can't be found any more, the chunk itself is used. The FTS fallback
uses the same rendering.
