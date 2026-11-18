# Always-on RoboRobo: lines as the unit of work

Status: draft for discussion, 2026-11-18. Nothing here is built.

## Why change

RoboRobo works. sequel has run on it for 13 nights. But four problems have
shown up:

- **Lines tangle.** sequel now has 18 lines of investigation, most of them
  branching from a few roots, and 129 experiment records. One overnight
  session gets all of them: the direction, every line's history, tonight's
  instruction and the checker's open challenges. The agent has to choose
  among lines, switch context between them, and keep them separate in one
  long conversation. It isn't always able to.
- **The morning bolus.** Everything arrives at once at 7 am, in one debrief
  covering every line, whether or not a line needs a decision.
- **Fixed schedule.** A session is 11 pm to 5 am. Work can't start when a
  job finishes or when you answer a question at 2 pm, and a session that
  submits a long job either waits for it or leaves it for tomorrow night.
- **Tied to one tool.** Sessions, checker and debrief are Claude Code
  scheduled tasks on one Mac. Every role uses the same provider, and there
  is no place to put a different model on one role (say, OpenAI as checker)
  or to run one line's work on Sterling and another's locally.

## The idea

Make the **line**, not the program, the unit of work, and run lines from a
small always-on service instead of scheduled sessions.

- A program is still one repository with one direction, one referee and one
  ledger. It becomes the **shared context** for its lines.
- Each line has its own memory, its own git branch and its own runs. A run
  sees the program's shared files and its own line, not the other lines'
  working state.
- Runs are started by **events** (a timer, a finished job, your reply, a
  verdict, an upstream line's result), not by the clock alone.
- Each **role** (worker, checker, referee, debrief) is set to a backend and
  model in configuration, per program and overridable per line.
- Each line names the **host** its compute runs on (local, Sterling, Ashe).
- Lines **report when they have something**. You get a digest at times you
  choose, and a decision request only when a line can't move without you.

The architecture follows Anthropic's
[Building always-on agents](https://claude.com/resources/articles/building-always-on-agents):
an agent that sleeps, wakes for a run, saves state and sleeps again, around
five pieces (event inbox, timers, memory, permissions, outbox). That article
describes one agent per inbox. Running several lines in one program, sharing
hosts and budgets, and keeping the referee independent are ours to design;
those are most of this document.

## Goals

- One line's run never has to reason about another line's working state.
- You review per line, in small pieces, and only decide what needs you.
- Work starts when there is something to do: a job ends, you answer, a
  timer fires.
- Any role can use any provider. Compute placement is per line.
- Everything RoboRobo enforces now still holds: your files are yours, plans
  before results, a referee the agent can't touch, a checker that never saw
  the agent's reasoning, `program check`.
- Fail loudly. A run that can't start, a backend that errors, a job that
  vanishes: each becomes a visible blocker, never a silent retry loop.

## Not goals

- A general agent framework. The pipeline is small and fixed (see
  [Why not LangChain](#why-not-langchain-or-langgraph)).
- Writing our own agent loop for every provider. Workers are existing agent
  harnesses we launch; we don't reimplement file editing and tool use.
- Real-time chat with lines. You talk to lines through replies and the
  debrief, as now.
- Running many programs on a shared multi-user service. One scientist, a
  few programs.

## Words

Existing words (program, direction, referee, claim, checker, debrief,
blocker, line) keep their meaning. New ones:

- **Run**: one wake-up of one role for one line. Usually minutes to an hour,
  not a night.
- **Event**: something that may need a run: a timer firing, a job finishing,
  your reply, a checker verdict, an upstream line's result.
- **Timer**: a time at which to enqueue an event. Either a follow-up the
  agent set ("check job 4411 at 03:00") or a schedule ("digest at 07:00").
- **Role**: worker (does the line's work), checker, referee, debrief.
- **Backend**: what carries out a role: an agent harness (Claude Agent SDK,
  Codex CLI, Claude Code headless) or a plain model API call.
- **Host**: where a line's compute jobs run: `local`, `sterling`, `ashe`.
- **Service**: the always-on process (`roborobo serve`) that holds the
  inbox, timers and outbox and starts runs.

## How it works

```
 timers ─┐
 job done┼─► inbox ──► dispatcher ──► run (role, line) ──► records + commit
 replies ┤   (SQLite)   │  checks:          │                   │
 verdicts┘              │  one run per line │                   ├─► new timers
                        │  host slots free  │                   ├─► job submissions
                        │  budget left      │                   └─► outbox
                        └───────────────────┘                        │
                                                    digest / decision requests ──► you
```

1. **Inbox.** Every event is a row in a SQLite table with a source ID (to
   drop duplicates), the program, the line it's for (or none), and a status
   (pending, taken, done, failed). Anything that wants a run writes an
   event; nothing calls an agent directly.
2. **Timers.** A separate table. The service polls it each minute and turns
   due timers into inbox events; schedules move to their next time.
3. **Dispatcher.** Takes pending events, groups them by line (three events
   for one line become one run), and starts a run when:
   - the line has no run in progress (exactly one at a time per line);
   - the line's host has a free slot, if the run will submit jobs;
   - the line and program budgets aren't exhausted;
   - the line is `active`.
   Events that can't start yet stay pending, with the reason recorded.
   Events for a line that arrive during its run wait for the next run.
4. **Run.** The dispatcher prepares the line's worktree, builds the
   context, launches the role's backend, waits, then runs `program check`
   and commits (see [A worker run](#a-worker-run)).
5. **Outbox.** A run ends in one of three ways: a note for you, nothing
   worth saying, or a decision request (the line can't move without you).
   Notes go into the next digest. Decision requests go out at the next
   digest unless the line is fully stopped, in which case they go out at
   once.

The service state (inbox, timers, runs, outbox) lives outside the program
repository, in the service's own directory. The program repository stays
the record of the research; the service database is the record of what ran
when, and can be rebuilt from the repository plus pending jobs if lost.

## Lines

### Memory per line

Today a line is records in `ledger/lines.jsonl`. It also gets a folder:

```
lines/L0013/
  question.md       the line's question, in your words where you asked it
  instruction.md    your current instruction for this line (yours)
  progress.md       what's done, what's next, jobs in flight (the agent's)
  reported.md       what was already sent to you, so it isn't repeated
```

A run of L0013 loads: `CLAUDE.md`, `DIRECTION.md`, `BRIEF.md`, `notes/`
(index only), `PROGRAM.md`'s summary of all lines (one paragraph each), and
everything in `lines/L0013/` and the line's upstream lines' `state`. It
reads other lines' records only on demand and never edits them.

### Branch per line

Each line works on a branch `line/L0013` in its own worktree, so two lines'
runs never touch the same checkout.

- At the start of a run the line branch is rebased onto `main`, so it sees
  shared context and other lines' merged results.
- At the end, after `program check` passes, the dispatcher merges the line
  branch into `main`. Ledger files are append-only JSONL, so they merge
  with git's union merge driver (`.gitattributes: ledger/*.jsonl
  merge=union`); a conflict anywhere else stops the merge and becomes a
  blocker.
- Record IDs can no longer come from `program next-id` on one branch: two
  lines would both take E0130. The service hands out IDs from its database
  (`program next-id` asks it), so they stay global and short.

### Dependencies between lines

`parent` and `feeds` already exist in `lines.jsonl`. With events they do
something: when a line's claim is accepted or the line closes, every line
it feeds gets an event. A line that is waiting on an upstream result sets
no timer and costs nothing until then.

### Keeping lines from growing without limit

Splitting lines makes them cheaper to run, which makes more of them. So:

- **A line opened by the agent starts `proposed`**, not `active`, and runs
  only after you approve it in a digest. Lines you open start active.
- **A cap on active lines per program** in `budgets.yaml` (say 6). Above it,
  a new line waits until you pause or close one.
- **Idle lines are flagged.** A line with no run in 7 days, or no progress
  after N runs, is listed in the digest with a suggestion: pause, close, or
  merge into its parent.

## Roles and backends

Configured in `program.toml` (yours), with per-line overrides:

```toml
[roles.worker]
backend = "claude-agent-sdk"
model = "claude-opus-5-5"

[roles.checker]
backend = "api"
provider = "openai"
model = "<an OpenAI model>"

[roles.debrief]
backend = "api"
provider = "anthropic"
model = "claude-sonnet-5-5"

[lines.L0013]
host = "sterling"

[lines.L0016.roles.worker]
backend = "codex"
```

Two kinds of backend:

- **Agent harness** (worker): an executable that runs an agent loop in a
  directory with tools for files, shell and the network. We launch it with
  a working directory, a prompt file and a permission profile, and read
  back the files it wrote. Claude Agent SDK, Claude Code headless (`claude
  -p`) and Codex CLI fit. Adding one means writing a launcher of about a
  page, not an agent loop.
- **Model call** (checker, debrief, some referees): a request with the
  materials attached and a structured answer back. The checker never needs
  to edit files, so it doesn't need a harness. This is where mixing
  providers costs nothing, and a checker from a different provider than the
  worker is more independent, not less.

A backend error fails the run and records a blocker with the backend's own
error. No fallback to another provider: if you want one, configure it.

## Hosts and compute

The agent's run happens on the service's machine. Its compute goes where the
line's `host` says.

```toml
[hosts.local]
kind = "local"
slots = 2

[hosts.sterling]
kind = "slurm"
ssh = "sterling"
slots = 20

[hosts.ashe]
kind = "slurm"
ssh = "ashe"
slots = 8
```

- Jobs are submitted through one command, `program submit --line L0013
  --host sterling -- <job script>`, not raw `ssh`/`sbatch`. It records the
  job against the line and experiment, takes a slot, and holds the SSH
  credentials, so the agent never needs them.
- When a job ends, the service enqueues a `job_done` event for its line.
  The service finds out by polling `squeue`/`sacct` over SSH every few
  minutes; a job can also report itself at the end of its script, but
  polling is the source of truth.
- So a line submits jobs and ends its run. It wakes when they finish. No
  run sits waiting on a cluster.
- Slots are a count per host, shared by all lines in all programs on that
  service. A line that can't get a slot stays pending, and the digest says
  so.

## Budgets

`budgets.yaml` gains per-line limits beside the program limits: model spend
(dollars, per role), compute (core-hours per host), and runs per day. The
service sums them from its run table and the ledger, warns at 50% and 80%
in the digest, and at 100% stops starting runs for that line and sends a
decision request. A per-run time limit (say 60 minutes) stops a run that
loops.

## Review: from bolus to a stream you control

- **Per line, small.** Each worker run that changed something writes a
  line note (the existing `note` record): two or three sentences, plus at
  most two questions with defaults.
- **Digest at your times.** `[schedule] digests = ["07:00", "14:00"]`. The
  digest groups notes by line, puts decision requests first, and lists
  lines waiting on slots, budget or approval. It can be empty; if so, it
  isn't sent.
- **Checker per run, not per night.** A run that added outcomes or claims
  gets a checker run right after it, which is an event like any other. The
  digest shows the verdict next to the note.
- **Answering.** You answer in the debrief conversation as now, or with
  `program reply L0013 "..."`. A reply is an inbox event for that line,
  which wakes it. "Go with your recommendations" works per line or for the
  whole digest.
- **The map stays.** `program map` already draws lines as rows; columns
  become days instead of nights, with a mark per run.
- **Unanswered questions** take their stated default after a time you set
  (default 24 h), as the fallback instruction does now.

Where the digest is read (a terminal, an emailed draft, a page) is an open
question. The first version writes it to a file in the program and the
debrief reads it.

## What stays enforced

- **Your files.** `program check` and approvals are unchanged. Line
  instructions (`lines/*/instruction.md`) become yours and are
  fingerprinted like `DIRECTION.md`.
- **The referee.** Runs can submit work to it and see only what they see
  today. Its code and hidden answers are not in any line worktree.
- **The checker's independence.** It gets the run's records and results,
  never the worker's transcript. With a different provider it also doesn't
  share the worker's model.
- **Plans before results.** Unchanged; registration and outcome are still
  separate ledger records, now on a line branch, and merge order keeps them
  in order on `main`.
- **Permissions.** Each worker backend gets the same deny rules RoboRobo
  renders now (no editing your files or RoboRobo's, no push, no approve, no
  raw cluster access). Where a backend can't express a rule,
  `program check` at the end of the run catches it, and the merge to
  `main` doesn't happen.

New risks to watch:

- More runs means more `program check` and merge steps; a broken merge rule
  could let a bad record through. Merges run `program check` on the merged
  result, not just the branch.
- An agent that opens many lines to get more runs. The proposed state and
  the cap handle it.
- Notes that repeat themselves across runs. `reported.md` and the digest's
  grouping handle it.

## Where the service runs

It has to be always on, which a laptop isn't, and it needs SSH to Sterling
and Ashe and the API keys. Options:

- a small RENCI VM (simplest; the service is light);
- a Sterling login node (probably not allowed for a long-lived process);
- a Mac that stays awake (works for one program, fragile as now).

This is the first decision to make; see [Open questions](#open-questions).

## Why not LangChain or LangGraph

- The graph is small and fixed: worker, checker, referee, digest, with
  events between them. A table and a loop express it plainly.
- Those frameworks wrap provider errors and retries in layers that make
  failures hard to see, against the fail-loudly rule.
- They want to own the agent loop. We want to launch existing harnesses
  that already do tools well, and only own what's between runs.
- Persisting state and resuming after a crash is the part they'd help with,
  and here it is an inbox table with statuses.

The service is plain Python: SQLite, `subprocess` for harnesses, each
provider's own SDK for model calls, run under `uv`.

## How to get there

Each phase is useful on its own, and the first tests the main idea cheaply.

1. **Run one line at a time** (inside today's setup). Add `program run-line
   L0013`: build the line's context, launch Claude Code headless in the
   line's worktree, run `program check`, merge. Add `lines/<id>/` and line
   branches. The nightly scheduled task becomes a loop over active lines.
   This alone tests whether separate lines work better.
2. **The service.** `roborobo serve` with the inbox, timers, dispatcher and
   outbox; `program submit` and job-done events; digests at set times;
   checker per run. Retire the scheduled tasks. Decide where it runs first.
3. **Backends and hosts.** Roles in `program.toml`, a second worker
   harness (Codex or the Agent SDK), model-call checker and debrief, host
   slots and per-line budgets.
4. **Review surface.** Wherever phase 2 shows the digest is awkward to read
   or answer.

sequel moves to each phase as it's built, as it did for the `program`
command.

## Open questions

- Where should the service run?
- Should agent-opened lines need your approval to start, or is a cap
  enough?
- Is one branch per line acceptable, or do some lines need to share code
  changes before they're merged (sequel's engine changes, for example)?
- How many digests a day, and where do you want to read them?
- Is a checker after every run worth its cost, or only after runs that add
  claims?
- Should a line be able to change its own host (move from local to Sterling
  when a pilot succeeds), or is that your decision?
