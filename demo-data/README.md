# /demo-data — Erik

The demo app, its test suite, the rules file, and the three planted PRs.

## What the engine needs from here

### 1. `demo-data/rules.md` — blocking

The judge loads this path and passes it to Claude as the standard to judge
against. Until it exists the engine falls back to three thin placeholder rules
in `engine/judge.py` (`FALLBACK_RULES`). Override the path with
`PR_GUARDIAN_RULES`.

**This file decides the verdicts.** A live run of the ambiguous PR currently
returns `allow`, not `escalate`, because the placeholder rules only cover
logging card data, committing credentials, and disabling TLS — none of which
the ambiguous diff touches. Real rules fix that.

### 2. A test suite the engine can run

The self-correction loop copies the repo to a temp directory, applies Claude's
rewritten files, and runs the tests there. It needs one command that exits
non-zero on failure and prints something useful on stdout/stderr.

### 3. The three planted PRs

| PR | Kind | Required verdict |
| --- | --- | --- |
| violation | app code logging a card number | `fix` |
| decoy | fake test card number in a **test** file | `allow` — catching it fails the demo |
| ambiguous | a change Guardian can't be sure about | `escalate` |

The decoy must carry the **same digits** as the violation. That pair is the
demo's whole argument: context beats keyword matching.

Stand-ins live in `engine/seed_fake_rows.py` (PRs 101/102/103) so the engine
could be built before this folder landed. Replace them with the real thing.

## Status

Not started.
