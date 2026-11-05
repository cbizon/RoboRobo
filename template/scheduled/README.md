# Scheduled tasks

<!-- Rendered from RoboRobo's template by `program sync`; don't edit here. -->

Prompts for the Claude Code (Code tab) scheduled tasks that run {{name}} on
{{scientist}}'s Mac. Create each task in the Code tab with "run only on this
computer", `{{repo}}` as its folder, and the prompt file's contents.

| Task | When ({{timezone}}) | Prompt |
| --- | --- | --- |
| Session | {{session_start}} until {{session_end}} | `session.md` |
| Checker | {{checker}}, about {{checker_minutes}} minutes | `checker.md` |
| Debrief | {{debrief}} | `debrief.md` |

The Mac has to be awake with the Claude app open. Long jobs run under
`caffeinate`, so they keep it awake once started; the start of each task is
the fragile part, and each session records how it actually ran.
