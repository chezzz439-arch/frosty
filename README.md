# frosty
PR Guardian for data: an agent that guards PRs that change SQL and data pipelines, built natively on Snowflake. A PR opens, Guardian reads the diff, judges it, and either fixes it, allows it, or escalates to a human. Every fix is tested in a zero-copy clone of the database before anything merges.
