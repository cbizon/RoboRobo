# How to work in this program

You are running one session of a long research program. Nobody is watching
this session; the scientist reads your summary tomorrow and replies with
tomorrow's instruction. Your memory between sessions is this repository.

## At the start

1. Read `DIRECTION.md` (the question, bounds, claim rules, autonomy,
   budgets). You never edit it, `referee/`, or `approvals.json`.
2. Read `PROGRAM.md`, then the instruction for today in `instructions/`.
   If none is dated today, follow `instructions/fallback.md`.
3. Read the last session summary in `sessions/` and any open records in
   `ledger/blockers.jsonl` and `ledger/claims.jsonl`.
4. Run `program check`. If it fails, fix what it reports before doing
   anything else, or stop and ask in your summary if you can't.

## While working

- **Pre-register.** Before running an experiment, append to
  `ledger/experiments.jsonl` with status `registered`: the hypothesis, the
  method, how the result will be judged, what would falsify it, and the
  cost estimate. Don't edit a registration once results exist; supersede it
  with a new record.
- **Check the measure varies.** Before spending a large budget on an
  experiment, run a small pilot to confirm the measure you'll judge by
  actually changes across conditions.
- **Record decisions.** Any method choice a reasonable scientist could make
  differently (parameters, thresholds, how to handle an odd case) goes in
  `ledger/decisions.jsonl` with your reasoning. If `DIRECTION.md` says a
  decision needs the scientist, record it as `proposed` and ask.
- **Record blockers.** Anything that stops progress goes in
  `ledger/blockers.jsonl` and stays open until resolved or carried forward.
- **Write evidence to `evidence/`**, named after the experiment id. Every
  number you report later must appear in one of these files exactly.
- **Use the referee for judgments.** Don't judge your own results as if you
  were the referee. Propose claims in `ledger/claims.jsonl`; the referee and
  the claim's evidence rules decide their status.
- **Watch the budget.** Note in your summary when a budget passes 50% and
  80%. At 100%, stop spending and ask.
- **Be a skeptic about your own results.** Before claiming anything, look for
  the most likely way it is wrong (leakage, a baseline that wasn't tuned the
  same way, a result driven by a few hubs) and check it.

## At the end

Leave time to write `sessions/<YYYY-MM-DD>.json` (schema:
`schemas/session-summary.schema.json`). Write it for a scientist who didn't
watch the session:

- `experiments`: for each one, the question, what you did, what happened,
  and what it means, in plain language. No internal ids or abbreviations in
  the prose; numbers in words ("about half the predictions were right").
- `claims_changed`: claims whose status changed, with why.
- `questions`: at most five, each with why it matters and what you'll do by
  default if unanswered. Only ask what the data can't settle.
- `proposed_directions`: two or three, ranked, each with a rationale.
- `budget`: what was spent and what remains.

Then update `PROGRAM.md` ("Current state" and "Next"), run `program check`,
and commit everything with a message describing the session.
