# Summary accuracy: what the original deployment learned

The digest (Stage 2) turns 30 minutes of chat into a few summary bullets, and recall injects
those bullets later as "what happened". If a bullet says the user *agreed* when they only
*asked*, or credits the user's idea to the assistant, the agent will later act on a false
memory. This page describes what the original author found while fixing that, and what went
into jevmem as a result.

**Read with care.** Everything below comes from one person's Korean chat logs, checked by an
AI (the assistant itself) comparing each summary claim with the raw chat — not blind, on small
samples. No figures are given here on purpose: the samples were too small for numbers to mean
much outside that one setup. No conversation text from those experiments is reproduced; the
examples in jevmem's prompt are invented.

## 1. Better labels did not mean correct summaries

A week of 30-minute summaries was rewritten with Claude Haiku and the resulting chunks were
relabeled. The labels got cleaner: fewer chunks piled into one catch-all topic. But when
randomly chosen summaries were checked line by line against the raw chat, many still had
errors: speakers swapped, content from different conversations mixed, wrong numbers.

## 2. Causes that were found and fixed

| finding | change | result on the same windows |
|---|---|---|
| Several 30-minute windows were sent in one call; content leaked between neighboring windows. | One window per call. | far fewer errors |
| Messages were cut at a few hundred characters; the model guessed the missing rest. | Longer lines, with an explicit "(truncated)" marker where a line is still cut. | — |
| Even with one window per call, Haiku still got a share of the claims wrong: numbers, names, status, invented content, speaker, omissions. | Full messages instead of cut lines. | fewer errors; invented content and guessed endings of cut lines disappeared |

## 3. Three ways to make a summary check itself

Tried on the same windows, all with Haiku:

1. **Cited lines.** The conversation is numbered line by line; each summary item comes back
   as structured data: who did it, its status (proposed / agreed to do / in progress / done /
   question / undecided), the text, and the numbers of the lines it is based on. Code then
   checks that the speaker appears in the cited lines, that every number in the text appears
   in the cited lines, and that "done" is backed by a line that actually reports it done.
2. **A verification pass.** A second call compares each summary sentence with the log and
   keeps, fixes or drops it.
3. **Trap examples.** The prompt shows short wrong/right pairs for the typical mistakes
   ("I'll do it" is not done; a relayed alert is not the assistant's action; an unanswered
   question is not an agreement; keep number/name pairs as written).

A combined variant (cited-line extraction plus a verification call) was then improved for
three gaps the first round exposed: **several speakers in one item**, **the same number
written differently** (a number word vs. digits), and **topics missing from the summary** (the
verification step may add one missed topic). It made fewer errors than the plain prompt.

A further variant added four rules: don't flip negations; a question or suggestion is not a
decision; the numbers in one sentence come from one line; attach a condition only to what it
was said about. Its results were mixed — worse on the windows used while writing the rules,
better on windows from another day it had not seen.

The samples were too small to choose between the two variants, so only the four rules went
into the production prompt; the two-call pipeline was not adopted.

## 4. The model mattered more than the method

With the improved prompt, Claude Sonnet made clearly fewer errors than Haiku on the same
windows, including in follow-up checks on a full day and on random windows from a week, at
the cost of more time per window. Older summaries written by a small local model (gemma) had
the most errors of all.

## 5. What jevmem does with this

- **One window per call.** Each `jevmem digest` run summarizes only the lines captured since
  the previous run; a window longer than `[digest] max_chars` is summarized in several calls.
- **(Nearly) full messages.** `[digest] max_line_chars = 1500`, `max_chars = 24000`; a cut line
  ends in `…(truncated)` and the prompt forbids guessing past it.
- **Speakers the model can't confuse.** Lines are labeled `User`, `Assistant`, or
  `Assistant (script)` for messages a script sent (`jevmem capture log-out`, e.g. alerts or
  relayed news). When several people write in one chat, user lines become `User (name)`.
- **Prompt rules** (see [capture-and-digest.md](capture-and-digest.md#default-prompt-digestprompt)):
  exact speakers; exact status (proposed / agreed / in progress / done / open question;
  "I'll do it" is planned; a question is not a decision; unanswered is not agreed; keep
  negations); numbers and names in the same notation and pairing; conditions only where
  they apply; no missing topics (group related ones instead); four invented trap examples;
  and a final "check every item against its lines" instruction.
- **The summary is an index, the chat is the source.** Recall adds a pointer line under each
  summary chunk (`↳ raw: jevmem raw 2026-09-10 09:34`), and `jevmem raw` prints the original
  lines of that window, so the agent can check a detail instead of trusting the summary.
- **Model choice is yours.** The default stays Haiku (`[llm] claude_model = "haiku"`); the
  checks above favored Sonnet, which is slower per window. `claude_model = "sonnet"` switches
  every `claude -p` job (summaries, labeling, consolidation).

Not adopted: the two-call extract-and-verify pipeline (twice the calls for a gain the
samples could not confirm).
