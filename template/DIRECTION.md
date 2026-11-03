# Direction: <program name>

Human-only. The agent reads this file and never edits it. After you change
it, record its new hash with `program approve DIRECTION.md`.

## The question

<One paragraph: what this program is trying to find out or produce, and why
it matters.>

## In scope / out of scope

- In: <...>
- Out: <...>

## What the program produces

<For example: a rule set; an annotated edge dataset with provenance and
confidence; simulation results and figures. Say where it ends up.>

## The referee

<What judges the work, and what the agent may see of it. Details live in
`referee/`. A program doesn't start until this exists.>

## Claim classes and their evidence

Every claim the agent proposes has one class. A claim is admitted only when
its required evidence exists; it can never rest on forbidden evidence. When a
claim falls under several classes, the strictest rule applies.

| Class | Required evidence | Forbidden evidence |
| --- | --- | --- |
| empirical | referee result on held-out data; baseline run in the same setup with the same budget | agent-written tests; results on data the agent tuned on |
| descriptive | evidence files reproducing the observation | |
| interpretive | an empirical or descriptive claim it rests on, named | |
| <add program-specific classes> | | |

## Acceptance bar

<What would make the program's output good enough to use or release. Write
it before results exist.>

## Autonomy

- The agent may, without asking: <run experiments under budget, read the
  literature, ...>
- The agent must ask first: <changing the method of a running campaign,
  anything irreversible, anything outside scope, ...>
- The agent may never: <edit this file or referee/, contact anyone, ...>

## Budgets

Limits live in `budgets.yaml` (also human-only) so `program check` can
enforce them. The agent warns in its session summary at 50% and 80%, and
stops spending at 100% until you raise the limit.

## Cadence

- Sessions: <when, how long>
- Morning check: <time>
