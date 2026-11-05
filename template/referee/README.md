# Referee

Human-only. This directory holds what judges the program's work: a scorer, a
frozen test suite, a hidden gold set's evaluator, a blind-rating queue, or a
downstream benchmark. The agent can run what's here (for example
`referee/run`) but can't edit it, and can't see inside anything marked
hidden.

Hidden data (answer keys, gold splits) does not live in this repository at
all. Keep it where only the referee process can read it, and give the agent
an interface that returns aggregate results.

After any change here, commit it and run `program approve`.
