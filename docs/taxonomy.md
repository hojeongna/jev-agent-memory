# Writing your taxonomy

The taxonomy is the "library shelving" for your agent's memory. Every log chunk is filed
under one subtopic at index time; every message is routed to a subtopic at query time,
and only chunks on that shelf (plus rare-word hits) become candidates. A taxonomy that
matches how *your* logs actually talk is the biggest quality lever you have.

## File format

`.jevmem/taxonomy.json` (path configurable as `[data].taxonomy`):

```json
{
  "version": "my-taxonomy-1",
  "smalltalk_topic": "smalltalk",
  "labeling": {
    "work": {
      "desc": "The user's job: meetings, tasks and deadlines, ...",
      "sub": {
        "meetings": "Meetings and calls: agendas, meeting notes, decisions, action items, ...",
        "tasks-deadlines": "Assigned tasks, priorities, deadlines, ..."
      }
    },
    "smalltalk": {
      "desc": "Casual conversation that needs no past memory: greetings, thanks, ...",
      "sub": { "greetings-thanks": "...", "chitchat": "..." }
    }
  },
  "routing": {
    "work": {
      "desc": "job, meetings, tasks, work docs",
      "sub": { "meetings": "meetings, calls, meeting notes", "tasks-deadlines": "tasks, to-dos, deadlines" }
    },
    "smalltalk": { "desc": "casual chat, no memory needed", "sub": { "greetings-thanks": "greetings, thanks, ok", "chitchat": "jokes, reactions" } }
  }
}
```

Rules (checked by `jevmem doctor` and at load time):

- `labeling` and `routing` must have **exactly the same** topic and subtopic names.
- Names may not contain `/` (it joins `topic/sub`).
- Total subtopics ≤ 255 (Jev `choice` limit). 20–60 is a good range.
- `smalltalk_topic` must name one of the topics. Messages routed there get no memory.

## Two descriptions, two jobs

| | `labeling` | `routing` |
|---|---|---|
| used | once per chunk (index time) | every message |
| style | long and concrete: the nouns, project names, typical events that appear in your logs | a few keywords |
| why | long descriptions made labels reproducible (re-labeling gave the same labels far more often than with short ones) | short descriptions routed as well or better and use fewer tokens per message |

Tips:

- Write labeling descriptions from the *log's* point of view ("Debugged the nightly job…"),
  routing descriptions from the *user message's* point of view ("bugs, errors, why did X fail").
- Put proper nouns that recur (project names, tools, places) into the labeling description of
  the subtopic they belong to. Rare-word matching catches one-offs; the taxonomy should
  catch the recurring ones.
- Make the smalltalk description explicit about "needs no past memory". It is the only
  thing that stops lookups for "thanks!" and "good night".
- Avoid near-duplicate subtopics across different top-level topics; the routing probability
  gets split between them.

## Drafting it with an LLM

Jev cannot write text, so let a text model draft the taxonomy from a sample of your logs.
For example, give Claude ~20 recent daily log files and this prompt:

```
You are designing a topic taxonomy for an AI assistant's memory. Below are sample daily
logs the assistant wrote about its user. Propose 6-12 top-level topics and 20-60 subtopics
that cover what these logs are actually about, plus one topic "smalltalk" for messages
that need no past memory.

Output JSON with this exact shape:
{"version": "...", "smalltalk_topic": "smalltalk",
 "labeling": {topic: {"desc": long, "sub": {sub: long}}},
 "routing":  {topic: {"desc": short, "sub": {sub: short}}}}

- labeling descriptions: 1-2 sentences, concrete, using the nouns, project names and
  event types that appear in the logs.
- routing descriptions: 3-6 keywords a user might use in a message.
- Names: lowercase, hyphens, no "/". Same names in labeling and routing.
- Prefer topics by life area/project over by emotion or by format.

<logs>
...paste logs here...
</logs>
```

Then:

1. Save it as `.jevmem/taxonomy.json` and run `jevmem doctor` (a JSON syntax error is
   reported with its line and column).
2. `jevmem index --dry-run` to see how many chunks and Haiku calls (or, with
   `labeler = "jev"`, Jev calls) labeling will take, then `jevmem index`.
3. `jevmem index --stats` shows chunk counts per topic. A topic holding a large share of
   all chunks should probably be split; a near-empty one merged.
4. Try a few real questions with `jevmem search "..."` and read the `route` field.

## Changing it later

Labels are stored per chunk, so the taxonomy is effectively a schema:

- Only descriptions changed, names unchanged: labels still work; relabel when convenient.
- Names added/removed/renamed: relabel the affected part.
  - `jevmem index --relabel-topic work --dry-run` then without `--dry-run`, or
  - `jevmem index --relabel --dry-run` for everything.
- Relabeling marks chunks stale; old labels keep working until each chunk is relabeled.
  Every run labels at most `[index].cap` chunks (default 400), so a large relabel is spread
  over several scheduled runs instead of one burst.
  - With the default **Haiku** labeler that cap is the only limit (~20 chunks per
    `claude -p` call, so up to ~20 calls per run with your own `claude` login). If
    `claude -p` itself fails (network, login, usage limit), labeling pauses and resumes by
    itself; those failures don't count against the chunks.
  - With `labeler = "jev"` (and for chunks that fall back to Jev), each run is additionally
    trimmed to the call budget limits `max_cost_usd_per_run` and `max_cost_usd_per_day`. A Jev
    call that gets no answer (timeout, connection error, HTTP 5xx/429/402) stops that run's Jev
    labeling without counting against the chunks.
  - A chunk whose relabeling comes back unusable `[index].max_tries` times keeps its old
    label and is not retried until `jevmem index --retry-failed`.

A useful habit: every few weeks, look at `.jevmem/inject_log.jsonl` for messages routed to
smalltalk that shouldn't have been, and for low top-topic probabilities. Recurring new
words there are the signal for a new subtopic.
