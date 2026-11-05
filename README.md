# RoboRobo

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

Working, with one program running on it (sequel, a granular-physics
simulation program). Built: the `program` command (`new`, `sync`,
`approve`, `check`, `next-id`), the starting files (`template/`), the record
formats (`schemas/`), the scheduled prompts for the session, checker and
debrief, and a library for judging a candidate against a baseline across
several quantities (`roborobo.judge`). Not built yet, and marked *(planned)*
below: checking that every number in a summary traces to a results file,
the `/program` slash command (the debrief prompt does its job for now), and
setting up the scheduled tasks automatically (you create them in the Code
tab). `LOG.md` records each change and the program that prompted it.

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

### Once: install the tool

```bash
uv tool install --editable ~/Projects/experiments/research-programs
```

This gives you the `program` command. A program written in Python can
instead add RoboRobo as a dependency (`uv add --editable <path>`), which also
gives its code `roborobo.judge`.

### Starting a program

1. **Create its repository**:

   ```bash
   program new ~/Projects/experiments/gene-disease-direction \
       --name gene-disease --title "which genes cause disease by loss or gain of function" \
       --scientist Chris --data-dir ~/Projects/experiments/gene-disease-data
   ```

   This makes a new git repository with the starting files and a
   `program.toml` holding the name, data folder and schedule (sessions 11 pm
   to 5 am, checker 5:30, debrief 7, by default; edit it and run
   `program sync`). Add a GitHub remote and push.

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

   Then `BRIEF.md`: how this program works day to day (commands to run at
   the start of a session, where code goes, how to run the referee's tools,
   what a fair baseline is). Claude drafts; you decide.

3. **Build the referee**, also with Claude, in ordinary interactive sessions
   (not scheduled ones). It goes in `referee/`. For a program without a ready
   standard, building the referee is real work: for example, assembling a set
   of gene–disease cases whose direction is well established, then holding
   part of it back for scoring. Check a sample yourself before trusting it.
   Scheduled sessions start only once the referee exists.

4. **Approve your files**: commit them, then

   ```bash
   program approve
   ```

   This records a fingerprint (a file hash) of each of your files in
   `approvals.json`, commits it, tags the commit `approved/<time>`, and pushes
   the tag to GitHub. If anything changes those files afterwards, including
   the agent, `program check` fails until you approve again. The check
   compares against the tag on GitHub, not a local copy, so an agent that
   rewrote `approvals.json` locally still fails; and it lists every approval
   from the last day, so one you didn't make shows up in the morning.

5. **Schedule it.** Create three Claude Code scheduled tasks in the Code
   tab (session, checker, debrief), set to run only on this computer, with
   the prompts in the program's `scheduled/` folder; its README gives the
   times. (Heavy programs can instead run sessions as cluster jobs, as
   miner2049er does on Sterling.) If
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
- **The checker reviews the session.** A separate session reads
  what was produced (not the agent's reasoning), tries to break each new
  claim, and records a verdict for each: holds, doubtful, or broken, with
  what would resolve the doubt. Open challenges are added to the next
  instruction, so the agent has to answer them.
- **The rules are checked.** The session runs `program check` at its start
  and end, and the checker and the debrief run it again, so a failure the
  session didn't report still reaches you. It confirms that your files and
  RoboRobo's are unchanged, that no ledger line was rewritten (through the
  git history), that every record matches its schema, that outcomes follow
  registrations, that open blockers are carried in the summary, and that
  budgets summed from the ledger aren't exceeded. *(Planned: checking that
  every number in a summary appears in a results file; for now the checker
  does it.)*
- **Your debrief is prepared each morning.** A scheduled task in the Claude
  Code app on your Mac runs the debrief prompt: it explains what was
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

Edit `DIRECTION.md`, `BRIEF.md`, `budgets.yaml` or the referee yourself,
commit, then run `program approve`.

### When RoboRobo improves

Update RoboRobo, then run `program sync` in each program: it re-renders the
files RoboRobo owns (the session brief, checker brief, scheduled prompts,
permissions). `program check` fails until you do, so no program silently
runs on old rules.

## What lives where

- **This repository is the tool**: the `program` command, the record
  formats, the starting files, and `roborobo.judge`.
- **Each program is its own repository**, because each one's git history is
  part of its audit trail (a plan committed before its results) and so an
  agent working on one program can't touch another.
- **Every file in a program has one owner**, and `program check` enforces
  each kind differently:
  - **yours** (`DIRECTION.md`, `BRIEF.md`, `budgets.yaml`, `program.toml`,
    `referee/`): fingerprinted by `program approve`;
  - **RoboRobo's** (`CLAUDE.md`, `CHECKER.md`, `scheduled/`, `.claude/`,
    the folder READMEs): rendered from `template/` and kept identical to the
    rendering by `program sync`; this is how improvements reach every
    program;
  - **the agent's** (`PROGRAM.md`, `ledger/`, `sessions/`, `evidence/`):
    validated against the schemas.
- **Large data** (simulation outputs, big datasets) lives outside git, in the
  program's data folder or on a cluster; evidence files record where, with a
  checksum.
- **What can't be enforced on a laptop.** Sessions run as you, so a shell
  command can write anywhere. The agent's permissions deny editing your
  files and RoboRobo's, pushing, tagging and approving; anything that gets
  past them is caught by `program check` the same morning, not prevented.

## Inside a program

This is what `program new` creates:

```
<program>/
  program.toml          yours: name, data folder, schedule, which files are yours
  DIRECTION.md          yours: the question, scope, claim rules, acceptance bar, autonomy
  BRIEF.md              yours: how the program works day to day
  budgets.yaml          yours: computing and spending limits
  referee/              yours: what judges the work, and referee/checker.md, the
                        program's own checks for the checker
  approvals.json        fingerprints of your files, written by `program approve`
  CLAUDE.md             RoboRobo's: the agent's standing instructions for every session
  CHECKER.md            RoboRobo's: the checker's standing instructions
  scheduled/            RoboRobo's: prompts for the session, checker and debrief tasks
  PROGRAM.md            the agent's: open questions, current state, next steps
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

- **Your files are yours, and RoboRobo's are RoboRobo's.** The agent never
  edits either; `program check` catches it if anything does.
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

- **sequel**, the first program run on RoboRobo, a granular-physics
  simulation program. From it: the `program` command with approvals anchored
  on GitHub, the split into general brief and program-specific `BRIEF.md`,
  run conditions for deciding where to run, keeping the machine clean, and
  the blocked-baseline pass rule in `roborobo.judge`.
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
