# Daily consolidation (profile memory)

Once a day (default 04:00, via `jevmem schedule`) `jevmem profile consolidate` distills
**at most 3 evidence-backed user traits** from one day's log into profile memory — like
memory consolidation during sleep. It is not the agent reflecting on itself: it only
extracts durable facts about the user.

```bash
jevmem profile consolidate                      # yesterday's log
jevmem profile consolidate --date 2026-09-02 --dry-run    # what would be sent; no LLM call, no writes
jevmem profile consolidate --date 2026-08-15 --backfill   # old logs: per-date cap only
```

`--dry-run` never calls the LLM: it prints the log's size (and how much of it would be sent),
the number of active traits and the remaining budget, then stops.

## What happens

1. Read `<logs>/<date>.md` (default: yesterday). No log, or a log that is empty or only
   whitespace -> nothing to do, no LLM call.
2. Send the configured text LLM (`[llm].backend`, falling back to `[llm].fallback`: `claude` in
   the isolated, plugin-free mode described in [capture-and-digest.md](capture-and-digest.md),
   or `ollama`) the day's log plus the list of existing active traits (with ids), asking for up
   to `[consolidate].max_traits` candidates as strict JSON. **Only the first
   `[consolidate].max_log_chars` characters of the log are sent (default 16,000; set it in
   `jevmem.toml`).** On a long day the later part is not seen, so traits that only show up
   there are not proposed. The quote check in step 3 still runs against the whole log.
3. Verify every candidate in code — the LLM's answer is only a proposal:

   | check | on failure |
   |---|---|
   | `kind` is one of preference, pattern, constraint, skill, relationship, goal | rejected |
   | statement is not a mood/emotion (mood words are rejected; moods may only go in `emotion`) | rejected |
   | `quote` occurs **verbatim** in that day's log (whitespace-normalized, ≥ 8 chars) | rejected |
   | `merge`/`supersede` target is an existing ACTIVE trait | treated as `new` |
   | a `new` trait whose closest existing trait has lexical similarity ≥ 0.6 | turned into `merge` |
   | a `new` trait that says the same as an earlier candidate of this run (same statement, or similarity ≥ 0.6) | merged into the trait that candidate just created (`skip` if it wasn't written) |
   | daily budget (3 per source date and per wall-clock day) | `skip` |

   The mood filter matches whole English words with their usual forms ("happier",
   "frustrating"; not "stress tests") and Korean stems ("답답", "괴로", "행복", "무서"; "화가"
   only as "화가 나다", so "화가 지망생" — an aspiring painter — passes).
4. Write through the same gateway as `jevmem profile new | merge | supersede` (idempotent,
   superseded traits kept for audit); the decisions are printed as JSON. The evidence is dated
   with the log's date (the day consolidated), never with a date that happens to appear in a
   folder name above the logs.

If the LLM can't be reached (for example `claude -p` is paused after an outage), nothing is
written and the run reports `llm-unavailable`; consolidation for that date is not retried
automatically, so run `jevmem profile consolidate --date YYYY-MM-DD` later.

## Default prompt

Configurable as `[consolidate].prompt` (placeholders: `{date}`, `{max_traits}`, `{language}`,
`{existing}`, `{log}`; literal braces must be doubled). The text below writes the literal braces
single, for reading: pasted into `jevmem.toml`, its JSON example becomes
`{{"traits": [{{"kind": "...", ...}}]}}`:

```
You maintain a small profile of a user, built from their AI assistant's daily log. Read
the log for {date} and propose at most {max_traits} DURABLE traits: things likely still
true in a month that should change how the assistant helps (kinds: preference, pattern,
constraint, skill, relationship, goal).
Rules:
- Never a mood or emotion as a trait ("was stressed"); a mood may only go in the optional
  "emotion" field.
- No one-off tasks or events, no facts about the world, no guesses.
- "quote" must be copied EXACTLY (verbatim substring) from the log.
- If a trait restates an existing one, use relation "merge" with its id; if it
  replaces/contradicts one, "supersede" with its id; otherwise "new".
- Write statements in {language}. Fewer is better; [] is fine.
Answer ONLY with JSON: {"traits": [{"kind": "...", "statement": "...", "quote": "...",
"emotion": null, "relation": "new|merge|supersede", "target_id": null}]}

<existing_traits>
{existing}
</existing_traits>

<log date="{date}">
{log}
</log>
```

## Privacy

The first 16,000 characters of the day's log (`max_log_chars`) and your existing traits are
sent to the configured LLM (`ollama` stays on your machine; `claude` goes to Anthropic through
your own `claude` login, with no tools in the child process). Consider what your logs contain; you can add rules to the
prompt, e.g. "never record health or financial details". The README's Privacy section lists
everything jevmem sends out and how to turn each part off.

## Auditing by hand

```bash
jevmem profile list
jevmem profile show 12              # full statement + every piece of evidence
jevmem profile delete 12 --commit   # soft delete a false positive (evidence kept)
jevmem profile inject --preview --query "a message you might send"
```

You can also write traits yourself (or from your own agent prompt) with
`jevmem profile candidates`, `new`, `merge` and `supersede`. These are the commands step 4
writes through, but on their own they check **less** than the daily job:

| check | daily consolidation | manual `profile new / merge / supersede` |
|---|---|---|
| `kind` is one of the six kinds | yes | yes |
| merge/supersede target is an ACTIVE trait | yes (else treated as `new`) | yes (else refused) |
| duplicate evidence is skipped; daily budget (3 per source date and per wall-clock day) | yes | yes |
| `quote` occurs verbatim in the log | yes (rejected otherwise) | **warning only**, and only when `--source` is an existing file |
| statement is not a mood/emotion | yes | no |
| a near-duplicate `new` becomes a `merge` | yes | no (use `profile candidates` first) |

`--source` is looked up as given, then relative to the config file's directory, then relative
to the current directory. The evidence date of a manual write is the `YYYY-MM-DD` in the
`--source` file's *name* (today if the name has none). A refusal for lack of budget says which
cap was hit: the per-date cap (3 pieces of evidence citing that date) or the wall-clock cap (3
written today, whatever dates they cite); `--backfill` skips only the latter, for old logs.
`--date` must be a real `YYYY-MM-DD` date (exit 2 otherwise).
