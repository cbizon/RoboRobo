# Scheduled runs

<!-- Rendered from RoboRobo's template by `program sync`; don't edit here. -->

{{name}} runs one line of investigation at a time. At {{session_start}}
({{timezone}}) `program run-night` reads the night's instruction and, for
each line it has a section for (and each line with open challenges from
the checker), runs the agent on that line, then the checker on that run.
It starts no new line after {{session_end}}.

| What | When | Prompt |
| --- | --- | --- |
| Night runner | {{session_start}}, launchd job | `run-night.plist` |
| One line's run | started by the runner, up to about an hour each | `line.md` |
| Checker, after each line | started by the runner, up to {{checker_minutes}} minutes | `line-checker.md` |
| Debrief | {{debrief}}, Code tab scheduled task | `debrief.md` |

## Installing the night runner

Once, on the Mac that runs the program:

```bash
cp {{repo}}/scheduled/run-night.plist ~/Library/LaunchAgents/org.renci.roborobo.{{name}}.plist
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/org.renci.roborobo.{{name}}.plist
```

After `program sync` changes the plist, copy it again and run
`launchctl bootout gui/$(id -u)/org.renci.roborobo.{{name}}` before the
bootstrap. The Mac must be awake at {{session_start}} (a wake schedule in
System Settings, or `pmset repeat`); once started, the runner keeps it
awake.

To see what tonight would run: `program run-night --dry-run`. To run one
line now: `program run-night --line L0013`.

## Where to look

- `{{data_dir}}/logs/YYYY-MM-DD/night.md`: what ran, for how long, and
  what went wrong. The debrief reads it.
- `{{data_dir}}/logs/YYYY-MM-DD/L0013-worker.log` and `-checker.log`: each
  run's output.
- `{{data_dir}}/logs/run-night.log`: the runner's own output, every night,
  including errors that stopped it from starting.

The worker and checker run as `claude -p` unless `program.toml` sets
`[runs] agent`. A run that fails, times out or leaves no summary is
reported, not retried. A run that leaves uncommitted changes or a failing
`program check` stops the night.
