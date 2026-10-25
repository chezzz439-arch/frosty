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

Judge the planted PRs for real on your laptop (needs `claude` logged in, or `ANTHROPIC_API_KEY`):
```
python trigger/send_diff.py --planted violation   # PR_DIFFS row #101 in the engine's local db
python engine/run.py 101                          # expect fix;      102 -> allow;  103 -> escalate
```
These are the same PR numbers as `engine/seed_fake_rows.py`, built from the real files.
**Phase 3 sign-off is Erik's:** decoy → `allow` citing R2 and ambiguous → `escalate` citing R3,
twice in a row each, live. If the decoy gets flagged, fix `rules.md` or the prompt, never the PR.

## Set up the demo repo (one time)

Why a separate repo: Guardian merges PRs by itself and `reset` force-pushes main. Neither should
happen to frosty.

1. `gh auth login`, then from frosty's root:
   `demo-data/make_demo_repo.sh <you>/frosty-demo-shop <kaushik-gh> <rohan-gh> <tejas-gh>`
2. Leave branch protection on main **off** (reset needs to force-push).
3. Repo **Variables**: `GUARDIAN_REF` = the frosty branch that has trigger + engine + merge
   (`main` once everyone's branches are merged in; that's the default).
4. Snowflake key-pair: `bash trigger/keygen.sh` and run the `ALTER USER` it prints in Snowsight.
5. Repo **Secrets**: `SNOWFLAKE_ACCOUNT` (`fqwenhp-ec45741`), `SNOWFLAKE_USER`, `SNOWFLAKE_PRIVATE_KEY`,
   optionally `SNOWFLAKE_ROLE`; **one of** `ANTHROPIC_API_KEY` or `CLAUDE_CODE_OAUTH_TOKEN`
   (`claude setup-token`); `SLACK_WEBHOOK_URL` (Tejas).
6. `demo-data/planted.sh open violation` → Actions tab: step 1 green.

## Between takes

```
demo-data/planted.sh reset        # close PRs, delete branches, main -> baseline
demo-data/planted.sh open decoy   # one at a time, on Rohan's cue
demo-data/planted.sh status
```
Run the decoy once before filming, so there's an earlier "allow" receipt for the memory shot.
