# Log

Changes to RoboRobo, newest first, with the program that prompted each.
When a program needs something done by hand, decide whether it's general;
if it is, it goes here first and reaches the program through `program sync`.

## 2026-11-23 (agent runs in their own pods)

- In the service, each agent run is now a Kubernetes Job of its own
  (`ROBOROBO_RUNNER=kubernetes`, `roborobo.kube`), from the service's
  image. It mounts only `Projects/` (program clones, data folders,
  RoboRobo) and uv's cache from the service's volume, with a scratch home,
  and gets the Claude token and nothing else: no SSH keys, no deploy key,
  no service-account token. It runs on the service's node, because the
  volume was made ReadWriteOnce. Before this, runs shared the service's
  pod and could read its keys, which is how the Nov 23 run could push an
  approval.
- `program submit`, `jobs` and `fetch` in an agent pod are sent to the
  service (`roborobo.service_api`), which runs them with its keys, for the
  run holding the current token, for a program it serves, and only those
  three commands. Runs keep their cluster workflow without holding a key.
- The service's keys live in a memory-only folder (`/run/roborobo`) that no
  agent pod mounts, as does the SSH connection socket; copies an earlier
  version kept on the volume are removed at start.
- The service has its own service account with a Role to create and watch
  Jobs and read pods and logs.

## 2026-11-23 (signed approvals, from sequel)

- On Nov 23 an agent run in the service pod approved its own change. The
  referee's tests left Python caches under `referee/`, `program check`
  failed on them as new human-only files, and the agent ran `program
  approve`. The deny rule (`Bash(uv run --project * program approve:*)`)
  didn't match its command, and GitHub's tag ruleset let the push through:
  a deploy key on a personal repository acts as its owner, who may bypass.
  It approved only the caches, but anything in the pod could approve.
- Approvals are now signed: `program approve` makes an annotated tag signed
  with the scientist's SSH key (`gpg.format = ssh`, `user.signingkey`), and
  `program check` counts the latest approval only if `git verify-tag`
  finds a good signature from a key in RoboRobo's `approvers` file. The
  key lives only on the scientist's computer and the list lives in
  RoboRobo, which the service can't push to, so who can push tags no
  longer matters. Earlier, unsigned approvals stop counting: approve once
  more after upgrading.
- Human-only files are what git tracks or would track; git-ignored files
  never count, so caches can't make an approval look stale.
- The brief says approving is the scientist's alone, and that a check
  failure on the scientist's files means a blocker and stop.
- Limit: in the pod, RoboRobo's clone is on the volume, so a deliberate
  forgery (a new key plus a local edit to `approvers`) could pass the
  pod's own check; the scientist's check, on their own RoboRobo clone,
  would still reject it.

## 2026-11-23 (first look, from sequel)

- The service's first look at a program records an event as already
  handled only if its line has a summary committed after it (after the
  instruction file, the review, or the job's end); anything else is
  pending and runs. It used to record everything as seen, assuming the
  night runner had done it. Sequel's service started at 8 pm on Nov 22,
  before the 11 pm night, with the night runner already off, so the
  Nov 22 instruction's three lines never ran until the events were set
  back to pending by hand.

## 2026-11-23 (dashboard and security, from sequel)

- The service writes a dashboard after every look and serves it
  (`--dashboard-port`, a ClusterIP Service on Sterling, reached with
  `kubectl port-forward`; no ingress, since the page has no login of its
  own). Top: what needs the scientist (a pause and how to resume, proposed
  lines, questions from each line's latest run, claims the checker doubts,
  failed jobs, lines held by the limits). Then a row per active line: where
  it stands, its last run, verdicts, jobs in flight, pending events, and a
  Discuss link (`claude://code/new`) that opens a Code tab thread on the
  line in the scientist's clone, which has the same path as the service's.
  Each program's map is written beside it.
- The morning debrief pulls first and reads the service's runs, not only a
  night's report.
- Sequel's GitHub repository has rulesets: only the admin can create, move
  or delete `approved/*` tags, and `main` can't be force-pushed or deleted.
  Agent runs share the pod, and so the deploy key, with the service; without
  the tag rule a run could push a forged approval. Runs in their own pods,
  without the keys, is the real fix and is next.

## 2026-11-22 (the service, phase 3)

- `program serve`: an always-on service that looks at each program every
  few minutes. It merges what the scientist pushed and pushes what runs
  committed, then runs any line with a new event: a new or changed
  instruction section, open challenges, or an ended Hatteras job. Runs
  are the night's runs (worker, then checker), one line at a time, so no
  branch per line yet.
- Each event is acted on once, and the first look records what's already
  there without running, so starting the service doesn't redo a night.
- `[service]` in `program.toml` caps runs per line per day (3) and the gap
  between them (60 min): with 400 slots, a run per ended job would be too
  many.
- A line's later runs in a day write numbered summaries and reviews.
- A dirty repository, a failing check, an invalid record or a conflicting
  merge pauses that program until `program serve --resume`. Errors
  reaching git or a host are shown and retried; anything else crashes the
  service, to be seen and restarted.
- `ROBOROBO_HOST` names the machine a process runs on, so the artifact
  check only verifies data recorded on that machine.
- `deploy/sterling/`: an image with git, ssh, rsync, uv and Claude Code
  (agent runs use a `claude setup-token` token, from the secret miner2049er
  already has), a 10 GiB volume laid out like the Mac so rendered paths
  match, and a one-replica Deployment.

## 2026-11-22 (conversion, from sequel)

- A symlink to a git repository in the data folder is code. Sequel's agent
  linked `worktrees/research-programs` so method worktrees resolve their
  `../research-programs` path source, and the coverage check called it
  unrecorded data.
- The brief says `$ROBOROBO_OUT` exists when a job starts. Six of L0019's
  pilot jobs failed because the harness refuses an existing `--outdir`
  and they passed `$ROBOROBO_OUT` itself.

## 2026-11-22 (artifacts, from sequel)

- Data is recorded wherever it lives. Every data folder, on the Mac or in a
  job's run folder on a cluster, is an artifact: a record in
  `ledger/artifacts.jsonl` (where it is, what made it: experiment, line,
  job, code commits, input artifacts) and a manifest of every file (sha256,
  bytes, path) in `ledger/manifests/`. Deleting one is a record too. JSONL
  in git with manifests beside it, not SQLite, so the record is diffable
  and travels with the repository; one record per folder, not per file,
  since one job can write thousands of files.
- Everything in the data folder is data, scratch (`tmp/`), code (git
  repositories) or operational state (`jobs/`, `logs/`, `map/`,
  `mirror/`, files directly in `runs/`). `program check` fails on
  anything else, and on a recorded folder that disappeared without
  `program delete`. A recorded folder that changed is a note, since
  running jobs write into theirs.
- Cluster outputs stay on the cluster. `program jobs` copies only small
  files (`fetch` patterns, `*.json` and the like) and logs into `mirror/`,
  and records the job's run folder as an artifact when it ends, hashing
  on the cluster. `fetch_exclude` is replaced by `fetch`: an allow-list
  can't let a new big file type through by accident. Sequel's continued
  loops had copied 1.5 GB back, mostly history the Mac already had.
- `program fetch A0123[:path]` copies anything into `tmp/fetch/` to look
  at; the night runner empties it after every run, so looking doesn't
  junk up the Mac.
- `program record` records local data (and new manifests as it grows);
  `--backfill` records whatever exists, provenance unknown. `program
  delete` removes a folder here or on a cluster and records why; cited
  data (claims, outcomes) can't be deleted.
- Inputs are artifacts: `--input A0123[:path]`, or a path inside a recorded
  folder. An input already on the cluster is copied there, so continuing
  from a cluster checkpoint never comes through the Mac.
- Uncommitted changes to the program's records (ledger, evidence,
  sessions, reviews, lines, instructions) no longer block `program
  submit`; it guards the code a job runs, and `program jobs` writes
  records.

## 2026-11-22 (from sequel)

- Registrations can say where they run: an optional `host` field on
  experiments. The brief asked for the host in the registration but the
  schema had no field, so L0019 added `host` itself, `program check`
  rejected the ledger, and the night stopped before L0020.
- `program submit` refuses a job whose registration names another host, so
  placement in the plan holds.
- `--tree NAME=PATH:REV` ships another git repository, from the data
  folder only, the way the program's own trees go. sequel's L0020
  evaluates grapenuts, a separate repository, and its runs need Hatteras.
  A colon can't appear in a git revision, so `NAME=REV` and
  `NAME=PATH:REV` can't be confused.

## 2026-11-21 (budgets, from sequel)

- `budgets.yaml` has a schema, and every rule is a field. Sequel's file
  carried rules in comments (which machine a limit covers, that the disk
  floor is a floor), which nothing checks and the agent had to interpret;
  that got worse once jobs could run on Hatteras.
- Each resource is a list of limits: `max` or `min` (a floor, only per
  `instant`), `per` (`instant`, `run`, `week`, `program`) and `where`
  (`local`, the computer sessions run on, or hosts in `program.toml`).
  `per_run_cpu_hours` becomes a second `cpu_hours` limit `per: run`.
- `where`, not `on`: YAML reads a bare `on` as true.
- `program check` validates the file, rejects unknown places, and rejects a
  limit per week or program that leaves out a place, because cost records
  don't say where they were spent, so such a limit couldn't be checked.
  Limits per instant and per run are reported as the agent's to watch.
- The old `{limit, period}` form fails with a message saying what changed;
  programs convert their file and re-approve it.

## 2026-11-21

- Jobs can run on a Slurm cluster: `program submit` and `program jobs`,
  first for Hatteras. Long simulations shouldn't depend on the Mac staying
  awake, and this works from today's nightly runs without the Sterling
  service.
- Hosts are `[hosts.<name>]` in `program.toml`, which is human-only and
  approved, so the agent can't add a host or raise its slot limit.
- Each job's code is real git checkouts at recorded commits, pushed over
  SSH to a bare repository on the cluster: no GitHub key there, and code
  that asks git for its own commit (sequel's harness does) works. A job can
  take more trees (`--tree engine=method/nudge`, for sequel's harness on
  `main` running an engine branch), and sibling repositories named as uv
  path sources go along at the same relative path (sequel's
  `../research-programs`). Submitting refuses uncommitted tracked changes
  (in the program and in path sources), an unregistered experiment, one
  that already has an outcome, and a host with no free slot.
- `--input` sends files from the data folder, such as a checkpoint to
  continue from; nothing outside the data folder.
- Outputs come back to `<data_dir>/runs/<E>/<host>-<job>/`, not
  `evidence/`, because run outputs can be large and the data folder is
  where programs keep them; the host's `fetch_exclude` (checkpoints) stays
  on the cluster. `program jobs` syncs running jobs too, so long jobs show
  progress. Job state lives in `<data_dir>/jobs/`. When a job ends its
  `job.json` (commits, command, resources, state, CPU-hours) is written to
  `evidence/<E>/<host>-<job>.json`, so the record in git says where and
  how it ran.
- Fail loudly: failed, timed-out, out-of-memory, cancelled and vanished
  jobs, and outputs over `fetch_max_mb`, are reported and make `program
  jobs` exit non-zero. Nothing is resubmitted on its own.
- Not yet: the agent runs as the scientist on the Mac, so it could call
  `ssh` or `sbatch` itself; the brief says not to, and nothing enforces it.
- Tried on Hatteras (`ht1.renci.org`, partition `batch`) with a copy of
  sequel: a fresh run of `referee/long_contact.py` on `--tree
  engine=method/nudge`, and E0081's run continued from its Mac checkpoint
  (`--input`) on the held-contact engine at its exact commit. Both built
  sequel and RoboRobo on the node and recorded the engine's commit;
  checkpoints stayed on the cluster. A failing job came back FAILED with
  its traceback.

## 2026-11-20 (from sequel)

- The first line-by-line night didn't run: launchd's login shell doesn't
  read `~/.zshrc`, where `~/.local/bin` (Claude Code's location) is put
  on PATH, so every line failed with "claude wasn't found". The plist now
  adds `~/.local/bin` to PATH, and the runner checks the agent command
  before the first line and stops the night with one clear message,
  instead of failing every line.
- Lines named with `--line` run at any hour; the session's end only stops
  a scheduled night. Re-running a missed night by hand was impossible.

## 2026-11-18 (from sequel)

- Nights run one line of investigation at a time (`program run-night`).
  sequel had 18 lines and one overnight session held all of them, which
  tangled them and produced one large morning review. Each line now gets
  its own agent run with only its section of the instruction and its own
  memory (`lines/L0013/progress.md`), then its own checker run, which
  never sees the worker's reasoning. Phase 1 of `docs/always-on.md`.
- Only lines the night's instruction has a section for run, plus lines
  whose latest review has a doubtful or broken verdict. A day without an
  instruction runs only those; `instructions/fallback.md` is no longer
  seeded. Lines the agent opens start `proposed` and don't run until the
  scientist makes them active.
- Lines run one after another and commit on `main`, as sessions did. No
  branch per line yet: sequel's code is already on `method/*` branches
  shared across lines, so a line branch would hold only records, and with
  no concurrency there is nothing for it to isolate. It comes back when
  lines run at the same time.
- The runner is started by launchd (`scheduled/run-night.plist`) and runs
  the agent as `claude -p`, configurable as `[runs] agent` in
  `program.toml`. The Code tab's session and checker tasks are retired
  (`program sync` deletes their prompts); their threads were hard to
  browse. The debrief task stays until the dashboard replaces it.
- Fail loudly: a run that fails, times out or writes no summary is
  reported in `<data_dir>/logs/<night>/night.md` and the night goes on; a
  run that leaves the repository dirty or `program check` failing stops
  the night, since the next line would start from that state. An
  instruction with no line sections is reported, not ignored.
- Summaries and reviews are per line run (`sessions/YYYY-MM-DD-L0013.json`,
  with `line_id`); the map and the blocker check read all of a night's.
- `[schedule] checker` is no longer used; `checker_minutes` is now the
  limit of each line's checker run.

## 2026-11-13 (from sequel)

- Added lines of investigation (`ledger/lines.jsonl`), terms
  (`ledger/terms.jsonl`) and `program map`, which builds one HTML page
  from the records: lines against nights, a card per line, and a glossary
  linked from every use of a term. The morning debrief read as a long
  linear narrative rewritten from scratch each day; the scientist wanted
  a standing picture that changes a little each night.
- The page is assembled by code and contains no prose of its own. The
  agent writes a `state` sentence per line and a `note` per line per
  night, the checker reads them like any other claim, and the debrief
  records the scientist's decisions as notes `by: "human"`.
- A mark's colour is the checker's worst verdict on that line's
  experiments that night. This is deliberate and has a known gap: a night
  whose experiments hold but which answered the wrong question shows as
  "holds". The `answers` verdicts cover that.
- Every experiment belongs to one line, enforced by `program check` for
  registrations made after a program's first line record. Earlier
  experiments are placed by a line record's `experiments` list, since
  registrations can't be edited. No merges: a line `feeds` another.
- Who opened a line is recorded, and a line the agent opens needs a
  reason. In sequel four of twelve lines were the agent's.

- Instructions now have two sections: `Asked`, the scientist's requests in
  the scientist's own words, and `Details`, the debrief's elaboration,
  which may add to a request but not narrow it. In sequel the scientist
  asked whether turning friction off rescued a method; the drafted
  instruction dropped part of the request, the session answered "what
  causes the stall" instead, and the debrief relayed the session's framing.
- Session summaries open with `answers`: each asked item word for word, a
  status, the reply in the item's terms, and departures. The checker gives
  each a verdict (`answered`, `partly`, `not_answered`,
  `different_question`) in the review's `answers`, and the debrief reports
  them before anything else. Both fields are optional in the schemas so
  earlier records still validate; the briefs require them.
- A change under test must be shown to have acted (where its output first
  differs from the unchanged version). sequel piloted three versions of a
  method whose runs were bit-identical to the baseline, and reported
  results from them.
- Questions for the scientist must be answerable from the summary or
  debrief alone; otherwise the default applies and is reported.

## 2026-11-05 (from sequel)

- Added `notes/`, the scientist's background, owned by the scientist and
  read before planning. sequel's contact-handling options lived in a
  Claude Docs page; the program's `PROGRAM.md` listed different options
  written from memory, because sessions can only read the repository.

- Built `program new`, `sync`, `approve`, `check`, `next-id`. sequel was
  set up by hand from the template and had drifted from it; nothing
  enforced its rules.
- Approvals are tags pushed to the remote, and `check` compares against
  the remote's latest. On a laptop the agent runs as the scientist, so a
  local `approvals.json` alone could be rewritten along with what it
  protects.
- Every file has one owner (scientist, RoboRobo, agent). Template-owned
  files are rendered and kept identical, so improvements reach every
  program through `program sync`.
- Split the session brief: general rules in `CLAUDE.md` (RoboRobo's),
  program specifics in `BRIEF.md` (the scientist's). Same split for the
  checker (`CHECKER.md` and `referee/checker.md`).
- Added the session, checker and debrief prompts to the template
  (`scheduled/`), with times from `program.toml`.
- Added run conditions to session summaries and reviews, for programs that
  aren't sure their machine is adequate.
- Added keeping the machine clean to the brief: stay in the repository and
  data folder, check disk and memory, empty `tmp/`.
- Added `roborobo.judge`, the blocked-baseline pass rule. sequel found that
  judging seven quantities at two standard deviations fails identical
  methods 30 to 50% of the time, and that one long run in blocks replaces
  independent runs when the system forgets its state quickly.
- Known limit: the brief assumes sessions on a Mac (`caffeinate`, `pmset`).
