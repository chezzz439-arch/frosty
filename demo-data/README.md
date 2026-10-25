# /demo-data — Erik

The demo app, its test suite, the rules file, and the three planted PRs.

```
rules.md            the standard the judge rules against (engine/judge.py loads this path)
shop/               the demo app ("Acme Shop" checkout) + pytest suite. Becomes the demo repo.
planted-prs/        violation, decoy, ambiguous: pr.md (branch, title, expected verdict, body) + files/
make_demo_repo.sh   one time: create the demo shop repo on GitHub with the Guardian workflow
planted.sh          open / status / reset the planted PRs between takes
test_demo_data.py   proves the PRs and tests behave the way the demo needs
```

## What the engine needs from here

| Need (from the scaffold) | Where | Status |
| --- | --- | --- |
| `demo-data/rules.md` | `rules.md` | ✅ R1 card data in logs → fix (with `mask_pan`), R2 published test numbers in `tests/` → allow, R3 payment-failure handling → escalate, R4 weakened tests → escalate, R5 secrets/TLS → escalate |
| One test command, non-zero on failure | `python -m pytest -q`, run from the demo repo root | ✅ 6 tests green on main |
| The three planted PRs | `planted-prs/` | ✅ below |

| PR | Branch | What it does | Required verdict |
| --- | --- | --- | --- |
| violation | `feature/log-card-used` | `checkout/payments.py` logs `card.number`; adds a test using `4111111111111111` that checks the log names the card | `fix` |
| decoy | `test/card-helper-tests` | new `tests/test_cards.py` with published test numbers, including `4111111111111111` | `allow` |
| ambiguous | `fix/checkout-timeout` | gateway timeout 30s → 3s, swallows processor errors and returns `"pending"`, rewrites the gateway test to match | `escalate` |

The violation and decoy carry the **same digits**. The violation PR even has them in its own test
file, so Guardian has to fix the app code and leave the test number alone.

### Why the self-correction retry is real

`tests/test_log_safety.py` (on main) fails while any full card number reaches the logs. The violation
PR's own test needs the log line to still say "Card used" with the last 4 digits. So a lazy first fix
fails on its own:

| Attempt | Result |
| --- | --- |
| PR as written | ❌ `test_log_safety` |
| delete the log line | ❌ `test_charge_logs_which_card_was_used` |
| log `"****"` | ❌ `test_charge_logs_which_card_was_used` |
| `mask_pan(card.number)` (what R1 says) | ✅ all green |

`planted-prs/01-violation/reference_fix/` is that last one, for `test_demo_data.py` only. Never give
it to the engine.

## Check it

```
pip install pytest
python demo-data/test_demo_data.py          # 9 checks, all OK
```

Judge the planted PRs for real on your laptop (needs `claude` logged in, or `ANTHROPIC_API_KEY`).
No Snowflake needed: the engine can use a local SQLite file. `--checkout` writes the PR's files to a
folder so the engine can read the whole file and run the tests on its fix:
```
export PR_GUARDIAN_DB_BACKEND=sqlite PR_GUARDIAN_TEST_CMD="python3 -m pytest -q"
python3 trigger/send_diff.py --planted violation --checkout /tmp/shop-101
PR_GUARDIAN_REPO=/tmp/shop-101 python3 engine/run.py 101     # expect fix, tests_passed
python3 trigger/send_diff.py --planted decoy --checkout /tmp/shop-102
PR_GUARDIAN_REPO=/tmp/shop-102 python3 engine/run.py 102     # expect allow
python3 trigger/send_diff.py --planted ambiguous --checkout /tmp/shop-103
PR_GUARDIAN_REPO=/tmp/shop-103 python3 engine/run.py 103     # expect escalate
```
(To use Snowflake instead, drop `PR_GUARDIAN_DB_BACKEND` and set `PR_GUARDIAN_SNOWFLAKE_CONNECTION`
to your snow connection name.) Same PR numbers as `engine/seed_fake_rows.py`, built from the real files.
**Phase 3 sign-off is Erik's:** decoy → `allow` citing R2 and ambiguous → `escalate` citing R3,
twice in a row each, live. If the decoy gets flagged, fix `rules.md` or the prompt, never the PR.

## Set up the demo repo (one time)

Why a separate repo: Guardian merges PRs by itself and `reset` force-pushes main. Neither should
happen to frosty.

1. `gh auth login`, then from frosty's root:
   `demo-data/make_demo_repo.sh <you>/frosty-demo-shop <kaushik-gh> <rohan-gh> <tejas-gh>`
2. Leave branch protection on main **off** (reset needs to force-push).
3. Nothing to set for `GUARDIAN_REF`: it defaults to frosty `main`, which has trigger, engine and merge.
4. Repo **Secrets** (values per `PROGRESS.md`; Kaushik's key is already registered in Snowflake):
   `SNOWFLAKE_ACCOUNT` = `FQWENHP-EC45741`, `SNOWFLAKE_USER` = `CHEZZZ439@GMAIL.COM` (the email,
   not the short name, or the key sign-in fails), `SNOWFLAKE_PRIVATE_KEY` = Kaushik's `rsa_key.p8`
   with the BEGIN/END lines, `CLAUDE_CODE_OAUTH_TOKEN` = output of `claude setup-token`
   (do **not** also set `ANTHROPIC_API_KEY`), `SLACK_WEBHOOK_URL` (Tejas).
   Kaushik pastes the key and token himself if he'd rather not share them.
   (`trigger/keygen.sh` is only for registering a separate CI key later.)
5. `demo-data/planted.sh open violation` → Actions tab: all steps green.

## Between takes

```
demo-data/planted.sh reset        # close PRs, delete branches, main -> baseline
demo-data/planted.sh open decoy   # one at a time, on Rohan's cue
demo-data/planted.sh status
```
Run the decoy once before filming, so there's an earlier "allow" receipt for the memory shot.
