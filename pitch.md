# PR Guardian — Tejas's 2-minute pitch

**0:00–0:20 — Introduction**
Hi, we're Kaushik, Erik, Rohan, and Tejas, and we built PR Guardian. It's an AI-powered pull request reviewer that doesn't just point out problems. It takes safe action, verifies the result, and leaves an audit trail.

**0:20–0:50 — Problem**
Every day, developers open pull requests that can accidentally introduce security risks or break working code. A single logging statement could expose a customer's card number. Human reviewers are busy, and traditional rule scanners can be too rigid: they might flag a harmless fake card number in a test file just because it looks suspicious. Teams need review that understands context, not just keywords.

**0:50–1:20 — Solution**
PR Guardian evaluates each pull request against project rules and makes one of three decisions: fix, allow, or escalate. For a clear violation, it generates a correction in a sandbox, runs the tests, and retries if necessary. It only merges when the fix passes. For legitimate test data, it allows the PR. And when confidence is low, it asks a human instead of guessing.

**1:20–1:45 — Technology**
Our GitHub Action starts the workflow. Claude analyzes the diff and reasons about the rules. Snowflake stores the decisions, event history, and receipts, and hosts our live Streamlit dashboard. GitHub receives the merge or escalation, while Slack notifies the team. Every outcome has an explanation that can be reviewed later.

**1:45–2:00 — Future**
Next, we'd expand Guardian across repositories, respond to new security advisories, and improve decisions using past receipts. PR Guardian is about making code review faster, safer, and accountable. Thank you.

*Before recording, confirm which capabilities have actually been demonstrated. Don't claim that a fix was merged unless the end-to-end run succeeded.*
