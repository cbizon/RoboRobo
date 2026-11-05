# Log

Changes to RoboRobo, newest first, with the program that prompted each.
When a program needs something done by hand, decide whether it's general;
if it is, it goes here first and reaches the program through `program sync`.

## 2026-11-05 (from sequel)

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
