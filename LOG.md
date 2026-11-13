# Log

Changes to RoboRobo, newest first, with the program that prompted each.
When a program needs something done by hand, decide whether it's general;
if it is, it goes here first and reaches the program through `program sync`.

## 2026-11-13 (from sequel)

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
