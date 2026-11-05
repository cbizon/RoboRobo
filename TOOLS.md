# Tooling (specified, not built)

The template only works if a few things are checked mechanically rather than
trusted. This is what to build, in order of need. Python, run with `uv`.

## `program` CLI

| Command | Who runs it | What it does |
| --- | --- | --- |
| `program new <dir>` | human | Copy `template/` into a new program repository, link the shared schemas, start git history. |
| `program approve <path>...` | human only | Record the SHA-256 of `DIRECTION.md`, `budgets.yaml`, or files under `referee/` in `approvals.json`. |
| `program check` | agent, every session start and end; scheduler | Fail loudly on anything below. |
| `program digest [--check]` | debrief skill; morning check | Render the latest session summary for a human, or print one line saying whether the scientist is needed. |
| `program next-id <E\|C\|D\|B>` | agent | Next free ledger id, so records don't collide. |

### What `program check` enforces

1. **Human-only files are unchanged since approval.** Hashes of
   `DIRECTION.md`, `budgets.yaml`, and `referee/**` match `approvals.json`.
   The `.claude/settings.json` deny rules stop the Edit and Write tools, but
   not a shell redirect, so the hash check is what actually holds the line.
2. **The ledger is append-only and valid.** Every JSONL line validates
   against its schema; the file's earlier lines are byte-identical to the
   last commit (git), so nothing was rewritten; ids are unique; every
   `supersedes` points to an existing record.
3. **Pre-registration came first.** Every experiment outcome has an earlier
   registration with the same id, committed before the outcome's evidence
   files first appeared (git timestamps).
4. **Admitted claims rest on the right evidence.** An `admitted` claim was
   admitted by the referee or the human, and every evidence path it cites
   exists. The class rules in `DIRECTION.md` are for the referee and the
   debrief to apply; the check enforces the parts a script can.
5. **Numbers trace to evidence.** Every number in a session summary's prose
   appears verbatim, or as an unambiguous rounding of one, in a file under
   `evidence/` cited by that experiment. Untraceable numbers fail the check
   and the summary has to be fixed.
6. **Budgets.** Sum `cost_actual` by resource and period against
   `budgets.yaml`; report 50% / 80% crossings; fail if a registration would
   exceed 100%.
7. **Blockers are conserved.** Every open blocker is listed in the latest
   session summary's `open_blockers`.
8. **The session summary is complete** and has at most five questions.

## Skills

- **`/program <name>` (debrief).** A generalisation of miner2049er's
  `/miner`: plain-language experiments first, then where things stand, then
  decisions with defaults and a recommendation, then problems. Answers "why"
  from `evidence/` and the ledger, checks against the scientist's taste
  file, drafts tomorrow's instruction, and writes it to `instructions/` on
  approval.
- **Checker session.** A scheduled session after each work session, with an
  adversarial brief: read the outputs (not the agent's reasoning), attack each
  new claim and any still-challenged one, and record `reviews/<date>.json`
  (schema: `review.schema.json`). `program check` refuses to let a claim be
  admitted while a challenge on it is open. Each program's `DIRECTION.md`
  lists its known failure modes for the checker to try.
- **Prepared debrief.** A Code-tab scheduled task on the Mac (not Cowork, whose
  scheduled tasks run in a sandbox without the user's tools) runs
  `/program <name>` each morning and waits for the scientist.

## Scheduling

Each program says in `DIRECTION.md` where its sessions run:

- **Light programs:** a Claude Code routine or a desktop scheduled task that
  opens the program repository and runs one session with the brief in
  `CLAUDE.md`.
- **Programs with long compute:** sessions as above, plus a job queue the
  agent submits to (Slurm on Hatteras, Kubernetes Jobs on Sterling).
  Sessions pick up finished jobs; jobs never run inside a session.
- **Programs whose referee must be sealed** (hidden answer keys): the
  miner2049er pattern on Sterling, with the referee in a separate pod.

## Not yet decided

- **Keeping the agent away from your tools.** `program check` and
  `program approve` only enforce anything if the agent can't run `approve` or
  rewrite `approvals.json`. On a cluster this is natural (the agent's pod
  doesn't get the approval tooling or write access, as in miner2049er's
  sealed volume). On a laptop it needs a separate account or a sandbox for
  sessions, or approvals kept somewhere the session can't write, such as a
  signed git commit checked by `program check`.
- **How the morning check reaches you** (desktop notification, email draft,
  message).
- Where the shared registry of programs lives (for the morning check across
  programs).
- How blind human ratings are collected (a small web form, or inside the
  debrief).
