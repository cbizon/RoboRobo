# Always-on RoboRobo: lines as the unit of work

Status: draft for discussion, revised 2026-11-18 after first review.
Phase 1 is built (`program run-night`), with two changes from the plan
below: lines commit on `main` instead of line branches, since they run one
at a time and code already lives on shared `method/*` branches; and only
instructed lines (and lines with open challenges) run. See `LOG.md`.

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
  scheduled tasks on one Mac, and browsing their threads in the scheduled
  tasks list is awkward. Every role uses the same provider, and there is
  no place to put a different model on one role (say, OpenAI as checker)
  or to run one piece of work on Sterling and another locally.

## The idea

Make the **line**, not the program, the unit of work, and run lines from a
small always-on service, a pod on Sterling, instead of scheduled sessions.

- A program is still one repository with one direction, one referee and one
  ledger. It becomes the **shared context** for its lines.
- A line is a question. It has its own memory, its own git branch and its
  own runs. A run sees the program's shared files and its own line, not the
  other lines' working state.
- Runs are started by **events** (a timer, a finished job, your reply, a
  verdict, an upstream line's result), not by the clock alone.
- Each **role** (worker, checker, referee) is set to a backend and model in
  configuration, per program and overridable per line.
- **Placement is per activity, not per line.** Answering one question may
  take a simulation on Sterling, an analysis next to data on Ashe and a
  quick check locally. Each job, and each agent run, says where it runs.
- **The dashboard is where you review.** It shows what happened per line
  and what needs you, and a button on any line or run opens a Claude Code
  thread to discuss it.

The architecture follows Anthropic's
[Building always-on agents](https://claude.com/resources/articles/building-always-on-agents):
an agent that sleeps, wakes for a run, saves state and sleeps again, around
five pieces (event inbox, timers, memory, permissions, outbox). That article
describes one agent per inbox. Running several lines in one program, sharing
hosts and budgets, and keeping the referee independent are ours to design;
those are most of this document.

## Decided so far

From the first review:

- The service runs as a pod on Sterling.
- Compute placement is chosen per activity (job or run), not fixed per
  line.
- At the start, placement is your decision: a line uses only the hosts you
  have allowed it, and asks you before using another.
- Lines the agent proposes need your approval before they run.
- One branch per line, with the concern noted below and a way to revisit.
- The checker runs after every worker run.
- No digests. The dashboard (the program map) is the review surface, with a
  button that opens a Claude Code thread for discussion. Scheduled-task
  threads go away.

## Goals

- One line's run never has to reason about another line's working state.
- You review per line, in small pieces, from one page, and only decide what
  needs you.
- Work starts when there is something to do: a job ends, you answer, a
  timer fires.
- Any role can use any provider. Any activity can run on any host you've
  allowed.
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
- Running many programs on a shared multi-user service. One scientist, a
  few programs.

## Words

Existing words (program, direction, referee, claim, checker, debrief,
blocker, line) keep their meaning. New ones:

- **Run**: one wake-up of one role for one line. Usually minutes to an hour,
  not a night.
- **Job**: compute a run starts and doesn't wait for: a simulation, a
  training run, a big query. It belongs to an experiment and a line.
- **Event**: something that may need a run: a timer firing, a job finishing,
  your reply, a checker verdict, an upstream line's result.
- **Timer**: a time at which to enqueue an event. Either a follow-up the
  agent set ("check job 4411 at 03:00") or a schedule.
- **Role**: worker (does the line's work), checker, referee.
- **Backend**: what carries out a role: an agent harness (Claude Agent SDK,
  Codex CLI, Claude Code headless) or a plain model API call.
- **Host**: somewhere work can run: `sterling`, `ashe`, `local`.
- **Placement**: which host a particular job or run uses, and why.
- **Service**: the always-on process (`roborobo serve`), a pod on Sterling,
  that holds the inbox, timers and run records, starts runs and serves the
  dashboard.

## How it works

```
 timers ─┐
 job done┼─► inbox ──► dispatcher ──► run (role, line, placement)
 replies ┤   (SQLite)   │  checks:            │
 verdicts┘              │  one run per line   ├─► records + commit + merge
                        │  host slots free    ├─► new timers
                        │  budget left        ├─► jobs (any allowed host)
                        └─────────────────────┤
                                              └─► dashboard: notes, verdicts,
                                                  decision requests ──► you
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
   - the host the run is placed on has a free slot;
   - the line and program budgets aren't exhausted;
   - the line is `active`.
   Events that can't start yet stay pending, with the reason shown on the
   dashboard. Events for a line that arrive during its run wait for the
   next run.
4. **Run.** The dispatcher prepares the line's worktree, builds the
   context, launches the role's backend where the run is placed, waits,
   then runs `program check`, commits and merges.
5. **Checker.** Every worker run that finishes is followed by a checker run
   on what it produced. The checker's verdict is an event for the line, so
   an open challenge wakes the line to answer it.
6. **Outcome.** A run ends in one of three ways: a note (shown on the
   dashboard), nothing worth saying, or a decision request (the line can't
   move without you). Decision requests sit at the top of the dashboard.

The service state (inbox, timers, runs, jobs) lives outside the program
repository, on the pod's volume. The program repository stays the record of
the research; the service database is the record of what ran where and
when, and can be rebuilt from the repository plus the clusters' job lists
if lost.

## Lines

### Memory per line

Today a line is records in `ledger/lines.jsonl`. It also gets a folder:

```
lines/L0013/
  question.md       the line's question, in your words where you asked it
  instruction.md    your current instruction for this line (yours)
  hosts.toml        hosts this line may use (yours)
  progress.md       what's done, what's next, jobs in flight (the agent's)
  reported.md       what was already shown to you, so it isn't repeated
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
- At the end, after `program check` passes on the merged result, the
  service merges the line branch into `main` and pushes. Ledger files are
  append-only JSONL, so they merge with git's union merge driver
  (`.gitattributes: ledger/*.jsonl merge=union`); a conflict anywhere else
  stops the merge and becomes a decision request.
- Record IDs can no longer come from `program next-id` on one branch: two
  lines would both take E0130. The service hands out IDs from its database
  (`program next-id` asks it), so they stay global and short.

**The concern.** A branch per line is clean for records and memory, where
lines really are separate. It is less clean for **code**. In sequel, lines
share one engine: a contact-handling change made in one line is what
several others need to test. Merging after every run keeps branches short,
so most of the time a change made in L0009 is on `main` before L0010's next
run. What can still go wrong:

- two lines change the same engine file in the same period and conflict;
- a line builds on another line's change that the checker later breaks;
- a line needs another line's change before that change is merged.

Mitigations, in order of cost:

- merge after every run (above), so drift is one run at most;
- a line can declare it **waits on** another line's merge, which is just an
  event;
- if conflicts recur, move shared code to its own line that owns it, with
  other lines requesting changes from it.

What would make us drop the idea: merge conflicts or "waiting on another
line's code" decision requests more than about once a week. Then the
fallback is one shared branch with per-line folders, and separation by
directory instead of by branch.

### Dependencies between lines

`parent` and `feeds` already exist in `lines.jsonl`. With events they do
something: when a line's claim is accepted or the line closes, every line
it feeds gets an event. A line that is waiting on an upstream result sets
no timer and costs nothing until then.

### Keeping lines from growing without limit

Splitting lines makes them cheaper to run, which makes more of them. So:

- **A line the agent opens starts `proposed`** and runs only after you
  approve it on the dashboard. Lines you open start active.
- **A cap on active lines per program** in `budgets.yaml` (say 6). Above it,
  a new line waits until you pause or close one.
- **Idle lines are flagged.** A line with no run in 7 days, or no progress
  after N runs, is marked on the dashboard with a suggestion: pause, close,
  or merge into its parent.

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
- **Model call** (checker, some referees): a request with the materials
  attached and a structured answer back. The checker never needs to edit
  files, so it doesn't need a harness. This is where mixing providers costs
  nothing, and a checker from a different provider than the worker is more
  independent, not less.

A backend error fails the run and records a blocker with the backend's own
error. No fallback to another provider: if you want one, configure it.

The morning debrief as a scheduled role goes away; its job moves to the
dashboard and the Discuss button (see [Review](#review-the-dashboard-and-a-thread)).

## Where work runs

A line is a question; answering it can take several kinds of work with
different needs. So placement belongs to each piece of work.

### Hosts

```toml
[hosts.sterling]
kind = "kubernetes"         # pods in namespace bizon, started by the service
slots = 20

[hosts.ashe]
kind = "<to confirm>"       # reached from the pod over SSH
slots = 8

[hosts.local]
kind = "pull"               # your Mac asks the service for work
slots = 2
```

The service runs on Sterling, so Sterling jobs are Kubernetes Jobs it
creates in your namespace. Ashe is reached over SSH from the pod. **Local**
can't be pushed to, since the pod can't reach your Mac: a small
`roborobo agent` on the Mac polls the service for work placed on `local`,
runs it, and reports back. While the Mac is asleep, local work waits, and
the dashboard says so.

### Jobs

- Every job is started through one command, `program submit --host ashe
  --experiment E0131 -- <job script>`, not raw `kubectl`, `ssh` or
  `sbatch`. It records the job against its line and experiment, takes a
  slot, and holds the credentials, so the agent never has them.
- The experiment's registration (written before it runs, as now) names
  where it will run and why: "Sterling, because 40 seeds × 6 h". So
  placement is in the plan, and the checker can question it.
- When a job ends, the service enqueues a `job_done` event for its line.
  The service finds out by watching job status on each host; a job can also
  report itself at the end of its script, but watching is the source of
  truth.
- A line submits jobs and ends its run. It wakes when they finish. No run
  sits waiting on a cluster.

### Runs

The agent's own runs need little compute, so by default they run in the
service pod. A run can be placed elsewhere when it has to work next to
something: analysing large outputs that live on Ashe, or using a tool only
installed locally. Then the service starts the harness on that host (a pod
on Sterling, an SSH session on Ashe, the local agent on the Mac) with the
line's worktree, and reads back the result the same way.

So the same line can run a simulation on Sterling, analyse its output with
a run placed on Sterling, then do a quick check locally, each placed where
it fits.

### Who decides placement

To start, you do:

- `lines/L0013/hosts.toml` (yours) lists the hosts a line may use, with
  any limits ("ashe, at most 4 jobs at once").
- Using a host not on the list is a decision request: the line says which
  host, for what, and why, and waits.
- Once the record shows which choices you always approve, some can move to
  the agent (for example, any allowed host for jobs under an hour).

## Budgets

`budgets.yaml` gains per-line limits beside the program limits: model spend
(dollars, per role), compute (core-hours per host), and runs per day. The
service sums them from its run table and the ledger, shows them on the
dashboard with warnings at 50% and 80%, and at 100% stops starting runs for
that line and makes a decision request. A per-run time limit (say 60
minutes) stops a run that loops.

## Review: the dashboard and a thread

No digests and no scheduled debrief. Two things replace them.

**The dashboard** is the program map, served live by the service instead of
written once a day:

- decision requests at the top: proposed lines, host requests, budget
  stops, merge conflicts, questions a line can't proceed without;
- one row per line, columns become days, a mark per run coloured by the
  checker's verdict, as `program map` draws nights now;
- a line's card: its question, where it stands, its runs with their notes
  and verdicts, jobs in flight and where, what it's waiting on;
- what changed since you last looked, highlighted, so you don't reread.

You can answer simple things there: approve a line, allow a host, accept a
default. Each answer is an event for that line.

**The Discuss button**, on each line, run and decision request, opens a
Claude Code thread on your Mac, in your local clone of the program
repository, already told what to look at: "Discuss line L0013 of sequel.
Read `lines/L0013/` and runs R0412–R0415 first." It is an ordinary Claude
Code conversation, so you can ask why and it can read the records. When the
discussion ends in an instruction, the thread writes it to
`lines/L0013/instruction.md`, you approve it as now, and the push wakes the
line.

The button is a link. The Claude desktop app answers
`claude://code/new?folder=<absolute path>&q=<prompt>`, which opens a new
Code tab session in that folder with the prompt filled in
([support article](https://support.claude.com/en/articles/14729294-open-claude-desktop-with-a-link)).
The dashboard builds it per line or run, with the folder set to your clone
of the program (from a setting on the dashboard) and a prompt that starts
with `git pull`, since the service pushes to GitHub and your clone may be
behind. Known limits:

- the app asks you to confirm the folder each time (links count as
  untrusted);
- the prompt is filled in, not sent; you press Enter;
- not yet checked on this Mac: whether a link opens a new session when the
  app is already open (one report says not, on Windows), and whether the
  browser asks before following a `claude://` link.

Fallback: `claude-cli://open?cwd=<path>&q=<prompt>` opens the same thing in
a terminal ([docs](https://code.claude.com/docs/en/deep-links.md)).

Unanswered questions take their stated default after a time you set
(default 24 h), as the fallback instruction does now.

## What stays enforced

- **Your files.** `program check` and approvals are unchanged. Line
  instructions and host lists (`lines/*/instruction.md`,
  `lines/*/hosts.toml`) become yours and are fingerprinted like
  `DIRECTION.md`.
- **The referee.** Runs can submit work to it and see only what they see
  today. Its code and hidden answers are not in any line worktree, and not
  on any host a line can place a run on.
- **The checker's independence.** It gets the run's records and results,
  never the worker's transcript. With a different provider it also doesn't
  share the worker's model.
- **Plans before results.** Unchanged; registration and outcome are still
  separate ledger records, now on a line branch, and merge order keeps them
  in order on `main`.
- **Permissions.** Each worker backend gets the same deny rules RoboRobo
  renders now (no editing your files or RoboRobo's, no push, no approve, no
  raw cluster access). Only the service pushes, with its own GitHub key.
  Where a backend can't express a rule, `program check` at the end of the
  run catches it, and the merge to `main` doesn't happen.

New risks to watch:

- More runs means more `program check` and merge steps; a broken merge rule
  could let a bad record through. Merges run `program check` on the merged
  result, not just the branch.
- An agent that proposes many lines to get more runs. Approval and the cap
  handle it.
- The service holds keys for GitHub, model providers, Sterling and Ashe.
  They live in Kubernetes secrets, mounted only into the service, never
  into run pods.

## The service on Sterling

- One Deployment, one replica (a second would break "one worker per
  inbox"), with a small persistent volume for the SQLite database and the
  program clones and worktrees.
- A service account that may create and watch Jobs and pods in your
  namespace, for Sterling jobs and placed runs.
- Secrets: model API keys, a GitHub deploy key per program repository, an
  SSH key for Ashe.
- The dashboard is served from the pod and reached the way you reach
  Sterling now (UNC VPN, then an ingress or port-forward).
- Needs outbound access to GitHub, the model providers and Ashe. To check
  with Sterling's network policy before building.
- Its own failures are visible: a heartbeat on the dashboard, and the
  dashboard saying plainly when the service isn't answering.

## Why not LangChain or LangGraph

- The graph is small and fixed: worker, checker, referee, with events
  between them. A table and a loop express it plainly.
- Those frameworks wrap provider errors and retries in layers that make
  failures hard to see, against the fail-loudly rule.
- They want to own the agent loop. We want to launch existing harnesses
  that already do tools well, and only own what's between runs.
- Persisting state and resuming after a crash is the part they'd help with,
  and here it is an inbox table with statuses.

The service is plain Python: SQLite, the Kubernetes and SSH clients for
hosts, each provider's own SDK for model calls, run under `uv`.

## How to get there

Each phase is useful on its own, and the first tests the main idea cheaply.

1. **Run one line at a time** (inside today's setup, on the Mac). Add
   `program run-line L0013`: build the line's context, launch Claude Code
   headless in the line's worktree, run `program check`, merge. Add
   `lines/<id>/` and line branches. The nightly scheduled task becomes a
   loop over active lines, with a checker after each. This alone tests
   whether separate lines work better, and whether branch per line holds
   up.
2. **The dashboard and Discuss button**, still reading the repository. The
   map becomes the review page and the debrief task is retired.
3. **The service on Sterling.** `roborobo serve` with the inbox, timers and
   dispatcher; `program submit` with Sterling jobs and job-done events;
   line runs in the pod. The dashboard moves into the service.
4. **More hosts and backends.** Ashe over SSH, the local agent on the Mac,
   runs placed on hosts, roles in `program.toml`, a second worker harness,
   a checker from another provider, per-line budgets.

sequel moves to each phase as it's built, as it did for the `program`
command.

## Open questions

- Does the `claude://code/new` link behave on your Mac with the app
  already open? (A five-minute test, before phase 2.)
- What kind of host is Ashe (Slurm, plain SSH, Kubernetes), and can a pod
  on Sterling reach it?
- Does Sterling's network policy allow a long-running pod outbound access
  to GitHub and the model providers?
- Does `local` mean your Mac, or is it enough to have Sterling and Ashe?
- Which placement decisions, if any, should move to the agent later, and
  on what evidence?
