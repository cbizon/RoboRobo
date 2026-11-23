# Tooling

Status (Nov 2026): `program new`, `sync`, `approve`, `check`, `map`,
`next-id`, `run-night`, `submit`, `jobs`, `record`, `delete`, `fetch` and `serve` are built (`src/roborobo/`). Of the checks below, 1, 2, 6, 7, 8 and 9 are
enforced; 3 is enforced as "an outcome follows its registration in the
ledger" (not yet by git timestamps of evidence files); 4 and 5 are not yet.
`program digest` and the `/program` skill aren't built; the debrief prompt
in `scheduled/debrief.md` does their job. Approvals are anchored as tags
pushed to the remote (see the README), which settles the first open
question below for laptops.

The template only works if a few things are checked mechanically rather than
trusted. This is what to build, in order of need. Python, run with `uv`.

## `program` CLI

| Command | Who runs it | What it does |
| --- | --- | --- |
| `program new <dir>` | human | Copy `template/` into a new program repository, link the shared schemas, start git history. |
| `program approve` | human only | Record the SHA-256 of every human-only file git tracks in `approvals.json` and push an `approved/*` tag signed with the scientist's SSH key; `program check` counts it only if the key is in RoboRobo's `approvers`. |
| `program check` | agent, every session start and end; scheduler | Fail loudly on anything below. |
| `program digest [--check]` | debrief skill; morning check | Render the latest session summary for a human, or print one line saying whether the scientist is needed. |
| `program map` | anyone | Write the program map, an HTML page built from the records: lines of investigation against nights, a card per line, a glossary. Default path `<data_dir>/map/index.html`; `--out` and `--nights` change where and how many nights. |
| `program next-id <E\|C\|D\|B\|L\|T>` | agent | Next free ledger id, so records don't collide. |
| `program run-night` | launchd, each evening; human, to run a line now | Run the lines the night's instruction has a section for (and lines with open challenges), one at a time, each followed by the checker. `--dry-run` says which and why; `--line` runs only the named lines; `--night` sets the date. Reports to `<data_dir>/logs/<night>/night.md`. |
| `program submit` | agent | Run one command as a Slurm job on a host in `program.toml` `[hosts]`, for a registered experiment. Code goes as git checkouts at recorded commits (`--tree NAME=REV` for more of this repository, `--tree NAME=PATH:REV` for another one in the data folder, uv path sources automatically), pushed over SSH; `--input` sends files from the data folder. Refuses uncommitted code, unregistered experiments, a registration naming another host, and full hosts. |
| `program jobs` | agent, each run | Poll `sacct`; copy small files (`fetch` patterns) and logs into `<data_dir>/mirror/`; record each ended job's run folder as an artifact (manifest hashed on the cluster) and its job record in `evidence/`; exit non-zero on any failed or vanished job. |
| `program record` | agent, human | Record a data folder here as an artifact, or a new manifest as it grows; `--backfill` records everything unrecorded. |
| `program delete` | agent, human | Delete an artifact, here or on a cluster, and record why; refused for cited data. |
| `program fetch` | agent | Copy a cluster artifact, or part of it, into `tmp/fetch/` to look at; the night runner empties it after each run. |
| `program serve` | the service (a pod on Sterling, or the Mac) | Every few minutes per program: merge what was pushed, push what runs committed, turn new instruction sections, open challenges and ended jobs into events, and run each line with pending events within `[service]` limits. Runs each line in a worktree of its own (branch `line/<id>`), up to `max_parallel_lines` at once, and merges clean runs into main; a broken run or a conflicting or check-failing merge pauses that line, an invalid record or a conflict merging what was pushed pauses the program; `--resume NAME[:LINE]` lets it run again. Hands out ledger ids. Writes and serves a dashboard (`--dashboard-port`). |

### What `program check` enforces

1. **Human-only files are unchanged since a signed approval.** The latest
   `approved/*` tag must be signed by a key in RoboRobo's `approvers`. Hashes of
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
6. **Budgets.** `budgets.yaml` validates against its schema, names only
   known places, and its limits per week or program cover every place; sum
   `cost_actual` by resource and period against those limits; report 50% / 80% crossings; fail if a registration would
   exceed 100%.
7. **Blockers are conserved.** Every open blocker is listed in the
   `open_blockers` of at least one of the latest night's summaries.
8. **The session summary is complete** and has at most five questions.
9. **Data is recorded.** Everything in the data folder is a recorded
   artifact, scratch, code or operational state; recorded local artifacts
   still exist unless deleted with `program delete`; manifests match their
   recorded hashes.

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
  agent submits to (Slurm on Hatteras via `program submit`, built;
  Kubernetes Jobs on Sterling, not yet).
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
