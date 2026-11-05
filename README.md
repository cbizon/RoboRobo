# research-programs

Run a research project with an AI agent (Claude Code) that works on a
schedule, typically overnight, while you check in once a day. Each project is
called a **program**. Examples: working out whether a gene's loss or gain of
function is what causes a disease, or running new experiments with a physics
simulator.

The point is results you can trust from an agent working largely on its own.
So the agent's work is judged by something it can't change, its plans are
written down before it runs them, and everything it does is recorded in files
you can inspect.

## Status

Draft. What exists: this README, the starting files for a new program
(`template/`), the formats for the program's records (`schemas/`), and a
specification of the tooling (`TOOLS.md`). What
doesn't exist yet: the `program` command-line tool, the `/program` slash
command, and the scheduling. Everything marked *(planned)* below describes how
it will work once built.

## Words used throughout

- **Program**: one research project, kept in its own git repository.
- **Session**: one scheduled run of the agent, for example 11 pm to 6 am.
- **Direction**: your written statement of what the program is for and the
  rules it runs under, in `DIRECTION.md`. Only you edit it.
- **Referee**: whatever judges the agent's results. The agent can submit work
  to it but can't change it or see inside it. Examples: a set of correct answers the
  agent never sees, with a script that scores against them; a fixed test suite;
  or you, grading a sample of outputs without knowing which method produced
  each.
- **Claim**: a finding the agent wants to assert, such as "for genes with a
  known loss-of-function variant, method A gets the direction right 90% of the
  time". The agent can only propose a claim; it counts once the referee or you
  accept it.
- **Instruction**: a short note from you telling tonight's session what to
  focus on. One per day, dated.
- **Checker**: a separate Claude session, after each work session, whose only
  job is to try to break the claims that session made. It never saw the
  agent's reasoning.
- **Debrief**: your daily conversation with Claude about the last session,
  prepared for you each morning, which ends with tomorrow's instruction.
- **Open question**: one of the program's research questions, listed in
  `PROGRAM.md`; it stays open until answered yes or no.
- **Blocker**: anything stopping progress (missing data, a failing tool, a
  decision only you can make), recorded until it's dealt with.

## Who judges what

Three different things look at the agent's work, and they shouldn't be
confused:

- **The referee decides** whether a result counts. It is independent of the
  agent: the agent can submit work to it but can't influence its verdict. In
  miner2049er this is the scorer, which checks proposed rules against
  treatments hidden from the agent.
- **The checker attacks.** After each session it tries to break the new claims
  (leakage, results carried by a few easy cases, unfair comparisons, a
  description that doesn't match what was actually done). It doesn't decide
  what's true; its challenges have to be answered, by the agent with
  evidence or by you, before a claim is accepted.
- **You steer.** The debrief is where you judge whether the work is going in a
  sensible direction and set the next instruction. Your judgment there isn't
  independent of the agent's framing, so it isn't the referee. When you do act
  as a referee, it's through blind rating, a separate, structured task.

## How you use it

### Once: install the tool *(planned)*

```bash
uv tool install <path to this repository>
```

This gives you the `program` command.

### Starting a program

1. **Create its repository** *(planned)*:

   ```bash
   program new ~/Projects/experiments/gene-disease-direction
   ```

   This makes a new git repository containing the starting files.

2. **Write the direction with Claude.** Open Claude Code in the new
   repository and fill in `DIRECTION.md` together:
   - the question and what's out of scope;
   - what the program should produce (a dataset, a rule set, figures);
   - the kinds of claim the program can make, and what evidence each kind
     needs before it counts. For example, "method A is more accurate than
     method B" needs both methods scored by the referee on data neither was
     tuned on;
   - the **acceptance bar**: the result that would make the output good enough
     to use;
   - what the agent may do without asking, and what it must ask about first;
   - the limits on computing time or money, in `budgets.yaml`.

   Claude drafts; you decide.

3. **Build the referee**, also with Claude, in ordinary interactive sessions
   (not scheduled ones). It goes in `referee/`. For a program without a ready
   standard, building the referee is real work: for example, assembling a set
   of gene–disease cases whose direction is well established, then holding
   part of it back for scoring. Check a sample yourself before trusting it.
   Scheduled sessions start only once the referee exists.

4. **Approve your files** *(planned)*:

   ```bash
   program approve DIRECTION.md budgets.yaml referee/
   ```

   This records a fingerprint (a file hash) of each in `approvals.json`. If
   anything changes those files afterwards, including the agent,
   `program check` (described below) fails until you approve the change.

5. **Schedule it** *(planned)*. Set up the recurring session (a Claude Code
   scheduled task, which runs Claude on a timer, or a job on a computing
   cluster for heavy programs) and the morning check described below. If
   you're not sure the place you picked is good enough (a laptop that may
   sleep, a small disk), have sessions fill in the optional
   `run_conditions` in their summaries and the checker assess them, and
   decide from that record after a couple of weeks.

### Every day

- **The session runs.** The agent reads your direction, today's instruction,
  and the program's records. It writes down each experiment's plan before
  running it, runs it, asks the referee to judge the result, and records what
  happened. It ends by writing a plain-language summary of the session with up
  to five questions for you, each with what the agent will do by default if
  you don't answer, and its ranked proposals for what to do next.
- **The checker reviews the session** *(planned)*. A separate session reads
  what was produced (not the agent's reasoning), tries to break each new
  claim, and records a verdict for each: holds, doubtful, or broken, with
  what would resolve the doubt. Open challenges are added to the next
  instruction, so the agent has to answer them.
- **The rules are checked automatically.** The scheduler runs
  `program check` before and after every session, outside the agent's control
  *(planned)*. It confirms your files are unchanged,
  that no record was rewritten, that each plan was written before its results,
  that every number in the summary appears in a results file, and that the
  budget isn't exceeded. If anything fails, the session stops and the morning
  check tells you.
- **Your debrief is prepared each morning** *(planned)*. A scheduled task in
  the Claude Code app on your Mac runs `/program <name>`: it explains what was
  tried, what happened and what it means, shows the checker's verdicts,
  recommends answers to the agent's questions, and drafts tonight's
  instruction. Then it waits. (Schedule it from the Code tab, set to run only
  on this computer; the app's other scheduling runs in a sandbox that can't
  reach your tools.)
- **You open it when you have time.** Everything is already there: reply "go
  with your recommendations", or discuss any point; it answers "why" by
  looking at the records. When you approve the instruction, it's saved for
  tonight.
- **If you skip a day**, tonight's session uses the standing fallback
  instruction (`instructions/fallback.md`): continue with the agent's own
  top-ranked proposal from last night, and use its stated default for every
  question you didn't answer.

### When you change course

Edit `DIRECTION.md`, `budgets.yaml` or the referee yourself, then run
`program approve` on what you changed.

## What lives where

- **This repository is the tool.** It will provide the `program` command, the
  record formats, the starting files, and the `/program` slash command. Each
  program depends on one fixed release of it, recorded in the program's own
  dependencies, so the rules don't change under a running program.
- **Each program is its own repository.** The starting files are copied in once
  and then belong to the program. Programs don't share a repository because
  each one's git history is part of its audit trail (for example, proving a
  plan was committed before its results), and so that an agent working on one
  program can't touch another.
- **Anything that has to be enforced is a command-line tool** (`program new`,
  `approve`, `check`, `digest`), because a rule the agent checks on itself
  isn't really enforced. That only holds if the agent can't run the tools
  that are yours: the scheduler, not the agent, runs `program check`, and
  sessions should run where the agent can't write `approvals.json` or run
  `program approve` (a separate account, or a container). How to arrange that
  on a laptop is an open question in `TOOLS.md`. `program digest` produces the session summary for a
  human; the morning check and the debrief both use it. **The parts that need
  conversation are a Claude Code slash command** (`/program`).
- **Large data** (simulation outputs, big datasets) lives on a cluster disk or
  in object storage; the program records where it is and a checksum.

## Inside a program

This is what `program new` creates from `template/`:

```
<program>/
  DIRECTION.md          yours: the question, scope, claim rules, acceptance bar, autonomy
  budgets.yaml          yours: computing and spending limits
  referee/              yours: what judges the work; the agent can run it, not edit it
  approvals.json        fingerprints of the three above, written by `program approve`
  PROGRAM.md            shared: the open questions, current state, next steps
  CLAUDE.md             the agent's standing instructions for every session
  instructions/         one dated instruction per day from your debrief, plus the fallback
  ledger/               the program's records; lines are only ever added, never changed:
    experiments.jsonl     each experiment's plan (written before running) and its outcome
    claims.jsonl          proposed claims and whether they were accepted
    decisions.jsonl       method choices (parameters, thresholds), who made them, and why
    blockers.jsonl        anything stuck, until it's resolved or explicitly carried forward
  sessions/             one plain-language summary per session
  reviews/              the checker's review of each session
  evidence/             the results, figures and data the records point to
```

## Why not just Claude Code conversations?

A Claude Code conversation waits for you, judges its own results, keeps its
memory in a conversation that grows and drifts, and decides what counts as
success after seeing the result. A program works while you're away, is judged
by a referee it can't change, keeps its memory in files any new session can
read, writes its plans down before running them, works within limits you set,
and reports in the same format every day. For quick exploration a conversation
is better. Programs are for weeks-long work whose results you need to trust.

## Standing rules

- **Your files are yours.** The agent never edits `DIRECTION.md`,
  `budgets.yaml`, `referee/` or `approvals.json`; `program check` catches it if
  anything does.
- **Plan, then run.** An experiment's plan (its hypothesis, method, how the
  result will be judged, what result would show the hypothesis wrong, and its
  expected cost) is recorded before any result exists. A changed plan is a new
  record that points to the old one.
- **Claims need the right evidence.** `DIRECTION.md` lists the kinds of claim
  the program can make, what evidence each kind needs, and what can never count
  (for example, tests the agent wrote itself). When more than one rule applies,
  the strictest wins.
- **Negative results are results.** An open question can be closed with the
  answer "no", and that is recorded.
- **Nothing stuck disappears.** A blocker is resolved, raised with you, or
  carried forward with a reason.
- **Numbers trace to evidence.** Every number in a session summary must appear
  in a results file, or the summary is rejected.
- **Budgets warn at 50% and 80%**, in the session summary. At 100% the agent
  stops spending and asks.
- **Challenges get answered.** A claim with an open challenge from the checker
  isn't accepted, and the agent must respond to the challenge in its next
  session: evidence that resolves it, a narrower claim, or withdrawal.
- **Pilot before spending.** Before a large experiment, the agent runs a small
  one to check that the thing it will measure actually varies.

## Kinds of referee

| Kind | What the agent sees | Example |
| --- | --- | --- |
| Hidden answer key | only overall scores | a set of known drug–disease treatments held back from the agent, scored by a separate service |
| Frozen test suite | pass or fail, and differences | a simulator must still reproduce the published results it was built from |
| Held-back gold set | scores on the held-back part | well-established gene–disease directions, part kept back for scoring |
| Blind rating by you | your ratings, afterwards | a daily sample of outputs, mixed with random controls, rated without knowing the method |
| Downstream test | the test's result | do the new annotations improve a separate prediction task? |

## Where the ideas came from

- **miner2049er**, an earlier nightly agent by the same author that searches a biomedical
  knowledge graph for rules predicting drug treatments, scored against hidden
  answers. From it: the daily rhythm, plain-language summaries, at most five
  questions with defaults, dated instructions with a fallback, the checker,
  and the lesson that the morning task should prepare the debrief rather than
  announce that one is due.
- **ResearchLoop** (Xia & Wang, arXiv 2605.28282): your-files-only direction,
  claims with explicit statuses, evidence rules per kind of claim, things that
  can never count as evidence, writing down what would prove a hypothesis wrong,
  closing questions as "no", and never dropping blockers. They rely on the agent
  following instructions; here `program check` enforces them.
- **AutoResearchClaw** (Liu et al., arXiv 2605.20025): numbers in reports must
  match recorded results, a measuring tool the agent can't edit, pilot runs,
  budget warnings at 50, 80 and 100%, and a separate skeptical review of
  results, which became the checker. miner2049er's checker is the first
  working version.

Tool details: [`TOOLS.md`](TOOLS.md).
