# research-programs

A generic process for long-horizon research projects run by AI agents, with
a human scientist checking in daily. A *program* is one project:
miner2049er rule mining, gene–disease mechanism direction, granular-media
simulations.

This is what separates a program from a set of Claude Code threads you check
on:

1. **Work happens while you're away.** A schedule starts each session; a
   thread waits for you.
2. **The agent doesn't grade its own homework.** Each program has a
   *referee* the agent can't edit or see inside.
3. **State lives outside the conversation.** A program document, an
   append-only ledger, and versioned evidence. Any fresh session resumes from
   them.
4. **"What counts" is decided before the results.** Experiments are
   pre-registered; claims carry evidence requirements fixed in advance.
5. **Budgets and gates.** Compute, scorer calls, money, and anything
   irreversible have limits or need your approval.
6. **One way of checking in.** Every program produces the same daily digest:
   what was tried, what happened, what it means, up to five questions for
   you, and the next move. If you don't answer, the agent follows its own
   top proposal and stated defaults.

## What a program is made of

```
<program>/
  DIRECTION.md          human-only: question, bounds, autonomy, budgets, bar
  PROGRAM.md            shared: approaches, research questions, current state
  CLAUDE.md             the agent's standing brief (copied from the template)
  referee/              human-only: what judges the work; agent can't edit
  instructions/         dated instructions from the daily check-in, + fallback
  ledger/               append-only records (JSONL, schemas in ../schemas)
    experiments.jsonl   pre-registrations and their outcomes
    claims.jsonl        claims and their status
    decisions.jsonl     method decisions, who made them, why
    blockers.jsonl      things stopping progress, until resolved or carried
  sessions/             one summary per session (the digest's raw material)
  evidence/             outputs the ledger points to: results, figures, data
  budgets.yaml          human-only: resource limits the checker enforces
  approvals.json        hashes of human-approved files (DIRECTION, budgets, referee)
```

Per program you write three things: `DIRECTION.md`, the referee, and the
tools or environment the agent works in. Everything else is shared.

## The daily cycle

1. **Session (scheduled).** The agent reads `DIRECTION.md`, `PROGRAM.md`,
   the latest instruction (or the fallback if none is dated today), and the
   ledger. It pre-registers experiments before running them, records
   decisions and blockers as they come up, writes evidence, and asks the
   referee to judge. It proposes claims; the referee's results and the
   claim's evidence rules decide their status.
2. **Session summary.** Before the session ends the agent writes
   `sessions/<date>.json`: plain-language experiments, claims that changed
   status, at most five questions with defaults, and ranked proposed
   directions.
3. **Check (scheduled, morning).** A one-line "does anything need you?".
4. **Debrief (you, when you want).** A Claude Code skill walks you through
   the summary in plain language, answers "why" from the evidence, and
   drafts the next instruction. You approve it; it is written to
   `instructions/<date>.md`.

## Rules every program follows

- **Direction is yours.** The agent never edits `DIRECTION.md`,
  `budgets.yaml`, `referee/`, or `approvals.json`. This is enforced (deny rules in
  `.claude/settings.json`, and the validator checks their hashes against
  `approvals.json`), not just asked.
- **Pre-register, then run.** An experiment record (hypothesis, method, how
  it will be judged, what would falsify it, cost estimate) is written before
  any result exists. Changing it afterwards means a new record that points to
  the old one.
- **Claims need evidence of the right kind.** Each claim has a class, and
  each class in `DIRECTION.md` names the evidence it needs and the evidence
  that can never support it (for example, tests the agent wrote itself).
  When several rules apply, the strictest wins.
- **Negative results are results.** A research question can close as
  *negative*; that is recorded, not dropped.
- **Blockers don't vanish.** A blocker is resolved, escalated to you, or
  carried forward explicitly.
- **Numbers trace to evidence.** Every number in a session summary must match
  a value in a file under `evidence/` exactly. The validator rejects a
  summary that doesn't.
- **Budgets warn at 50, 80 and 100%.** At 100% the agent stops spending and
  asks.

## Referees

The referee is the part that's project-specific by nature, and the part
worth your design time. Kinds, which can be combined:

| Kind | Example | Agent sees |
| --- | --- | --- |
| Hidden answer key | miner2049er's scorer | aggregate scores only |
| Frozen test suite | granular fidelity suite (reproduce the 1998 results) | pass/fail and diffs |
| Hidden gold split | gene–disease direction gold set | scores on the hidden part |
| Blind human rating | your morning ratings of a sample, with random controls | the ratings, later |
| Downstream benchmark | direction qualifiers tested by miner2049er | the benchmark's result |

A program with no referee is a thread with extra paperwork. Don't start one
until its referee exists, even if the first experiment's job is to build it.

## Where ideas came from

- From **ResearchLoop** (Xia & Wang, arXiv 2605.28282): the human-only
  direction file, the claim ledger and its statuses, per-class evidence
  contracts with "strictest wins", forbidden evidence, falsification
  conditions on research questions, negative closeouts, and conserved
  blockers. Their enforcement is by convention; here it is checked.
- From **AutoResearchClaw** (Liu et al., arXiv 2605.20025): numbers in
  reports must trace to logged outputs (exact match here, not 1%), a measuring
  harness the agent can't edit, a pilot check that the main measure varies
  before spending, a skeptic's review of results, and budget warnings at
  50/80/100%.
- From **miner2049er**: the daily cadence, plain-language summaries,
  at most five questions with defaults, dated instructions with a fallback,
  and the morning digest.

## Status

Draft. `template/` and `schemas/` are written; the tooling in
[`TOOLS.md`](TOOLS.md) is specified, not built. The first program planned on
this template is gene–disease mechanism direction.
