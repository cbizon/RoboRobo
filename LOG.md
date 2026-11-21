# Log

Changes to RoboRobo, newest first, with the program that prompted each.
When a program needs something done by hand, decide whether it's general;
if it is, it goes here first and reaches the program through `program sync`.

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
