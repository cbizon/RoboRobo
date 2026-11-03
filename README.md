# research-programs

Run a research project with an AI agent working on a schedule, while you
check in once a day. One project is a *program*: for example, inferring
gene–disease mechanism direction, or new experiments with the granular
simulator.

## Status

Draft. What exists: this README, the files a new program starts from
(`template/`), and the record formats (`schemas/`). What doesn't exist yet:
the `program` command-line tool, the `/program` debrief skill, and any
scheduling. The walkthrough below is how it is meant to work once those are
built.

## How you use it

### Once: install the tool

```bash
uv tool install ~/Projects/experiments/research-programs
```

This gives you the `program` command.

### Starting a program

1. **Create its repository.**

   ```bash
   program new ~/Projects/experiments/gene-disease-direction
   ```

   This makes a new git repository with the starting files from
   `template/`.

2. **Write the direction, with Claude's help.** Open Claude Code in the new
   repository and work through `DIRECTION.md` together: the question, what's
   in and out of scope, what the program should produce, how claims will be
   judged, the acceptance bar, what the agent may do without asking, and
   the budget in `budgets.yaml`. These are your decisions; Claude drafts,
   you decide.

3. **Build the referee.** Whatever will judge the work goes in `referee/`:
   a hidden gold set and the script that scores against it, a frozen test
   suite, a rating queue for you. Often the first job is building it, and
   you check it before trusting it. Don't start sessions until it exists.

4. **Approve.** You run:

   ```bash
   program approve DIRECTION.md budgets.yaml referee/
   ```

   This records fingerprints of those files. From then on, if anything
   changes them (including the agent), `program check` fails.

5. **Schedule sessions.** Set up the recurring session (a Claude Code
   scheduled task, or a cluster job for heavy programs) and the morning
   check.

### Every day

- **Overnight (or whenever it's scheduled), the agent works.** It reads
  your direction, today's instruction, and the program's records; plans
  experiments and writes each plan down before running it; runs them; asks
  the referee to judge; and ends by writing a plain-language summary with up
  to five questions for you. `program check` runs at the start and end and
  stops the session if a rule was broken.
- **In the morning, a one-line check** tells you whether a program needs
  you ("gene-disease-direction: 3 questions") or stays quiet.
- **When you have time, you debrief.** In Claude Code, run
  `/program gene-disease-direction`. It explains what was tried, what
  happened, and what it means; goes through the agent's questions with you;
  answers "why" from the records; and drafts tonight's instruction. When you
  approve the draft, it is saved for tonight's session.
- **If you skip a day**, tonight's session follows the agent's own top
  proposal and its stated defaults for your unanswered questions.

### When you change course

Edit `DIRECTION.md` (or the budget, or the referee) yourself, then run
`program approve` on it again. The agent never edits those files.

## What lives where

- **This repository is the tool.** It provides the `program` command, the
  record formats, the starting files, and the skills. Programs depend on a
  pinned version of it.
- **Each program is its own repository**, made by `program new`. The
  starting files are copied once and then belong to the program. Its git
  history is part of the audit (for example, that a plan was written before
  its results), so programs don't share a repository.
- **Command-line tools do anything that must be checked or enforced**
  (`program new`, `approve`, `check`, `digest`), because a rule that Claude
  enforces on itself isn't enforced. **Skills do the parts that need
  conversation** (`/program`, the debrief), and call the command-line tools
  for the facts.
- **Large data** (simulation outputs, graph builds) lives on a cluster
  volume or object storage; the program records where, with a checksum.

## Inside a program

```
<program>/
  DIRECTION.md          yours: question, bounds, claim rules, acceptance bar, autonomy
  budgets.yaml          yours: resource limits the checker enforces
  referee/              yours: what judges the work; the agent can run it, not edit it
  approvals.json        fingerprints of the three above, written by `program approve`
  PROGRAM.md            shared: research questions, current state, next steps
  CLAUDE.md             the agent's standing instructions
  instructions/         one dated instruction per day from your debrief, plus a fallback
  ledger/               the record, append-only:
    experiments.jsonl     each plan (written before running) and its outcome
    claims.jsonl          claims and their status; only the referee or you admit one
    decisions.jsonl       method choices, who made them, and why
    blockers.jsonl        what's stuck, until resolved or explicitly carried forward
  sessions/             one plain-language summary per session
  evidence/             results, figures and data the records point to
```

## Why not just Claude Code threads?

A thread waits for you, judges its own results, keeps its state in a
conversation that drifts, and decides what counts after it sees the result.
A program works while you're away, is judged by a referee it can't edit,
keeps its state in files any new session can pick up, writes its plans down
before running them, works within budgets, and reports to you in the same
format every day. For quick exploration a thread is better; programs are for
long-running work whose results you need to trust.

## Standing rules

- **Your files are yours.** The agent never edits `DIRECTION.md`,
  `budgets.yaml`, `referee/` or `approvals.json`; `program check` catches it
  if anything does.
- **Plan, then run.** An experiment's plan (hypothesis, method, how it will
  be judged, what would show it wrong, expected cost) is recorded before any
  result exists. A changed plan is a new record pointing to the old one.
- **Claims need the right evidence.** Each claim has a type, and
  `DIRECTION.md` says what evidence each type needs and what can never count
  (for example, tests the agent wrote itself). The strictest applicable rule
  wins.
- **Negative results are results.** A question can close as "no".
- **Nothing stuck disappears.** A blocker is resolved, raised with you, or
  carried forward with a reason.
- **Numbers trace to evidence.** Every number in a summary must appear in an
  evidence file; otherwise `program check` rejects the summary.
- **Budgets warn at 50% and 80%**, and at 100% the agent stops spending and
  asks.

## Referees

The referee is the part each program designs for itself. Kinds, which can be
combined:

| Kind | Example | What the agent sees |
| --- | --- | --- |
| Hidden answer key | miner2049er's scorer | aggregate scores only |
| Frozen test suite | granular simulator: reproduce the 1998 results | pass/fail and differences |
| Hidden gold set | gene–disease direction: a held-back part of the gold set | scores on the hidden part |
| Blind rating by you | a daily sample of outputs, mixed with random controls | your ratings, afterwards |
| Downstream benchmark | direction annotations tested by miner2049er | the benchmark's result |

## Where ideas came from

- **ResearchLoop** (Xia & Wang, arXiv 2605.28282): the human-only direction
  file, the claim record and its statuses, evidence rules per claim type
  with the strictest winning, forbidden evidence, falsification conditions,
  negative closes, and conserved blockers. They enforce these by convention;
  here `program check` does.
- **AutoResearchClaw** (Liu et al., arXiv 2605.20025): numbers in reports
  must match logged outputs (exactly, here), a measuring harness the agent
  can't edit, a pilot run to check the measure varies before a big spend, a
  skeptic's review of results, and budget warnings at 50/80/100%.
- **miner2049er**: the daily rhythm, plain-language summaries, at most five
  questions with defaults, dated instructions with a fallback, and the
  morning check.

Tool details: [`TOOLS.md`](TOOLS.md).
