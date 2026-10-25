"""The judge: a diff in, a fix/allow/escalate verdict out.

This is the part that has to be judgment rather than keyword matching. The
decoy PR in the demo -- a fake card number used as test data in a test file --
contains the same digits as the real violation. Catching it fails the demo. So
the rules go in the prompt as context, not as regexes.

Why structured outputs rather than a tool call: the Messages API can constrain
the whole response to a JSON Schema via `output_config.format`, which is one
round trip and leaves no chance of a text-only reply we have to re-prompt for.
A tool call would also work but needs the tool_use block extracted and can be
skipped by the model. Documented at
https://platform.claude.com/docs/en/build-with-claude/structured-outputs

The API client is injected so the judge is testable without a key or a network
call -- see engine/test_judge.py.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any, Protocol

from db import DECISIONS

# ASSUMPTION: Opus 4.8 is the right default -- this is the one step where a
# wrong call is expensive (a bad fix that merges), so we don't trade accuracy
# for cost here. Override with PR_GUARDIAN_MODEL.
DEFAULT_MODEL = "claude-opus-4-8"

# Non-streaming request, so keep max_tokens under the SDK's HTTP timeout
# guard. Rewritten files are whole-file contents, hence not small.
MAX_TOKENS = 16000

# ASSUMPTION: /demo-data holds the rules file (it's Erik's folder, per
# CLAUDE.md, and not written yet). Override with PR_GUARDIAN_RULES. If the file
# is missing we fall back to FALLBACK_RULES so the engine runs before
# /demo-data lands.
DEFAULT_RULES_PATH = os.path.join(
    os.path.dirname(os.path.dirname(__file__)), "demo-data", "rules.md"
)

# Deliberately thin. The real rules file is Erik's to write; this exists only
# so the judge has something to reason against today.
FALLBACK_RULES = """\
1. Never log, print or write a real payment card number, CVV, or full
   bank account number to logs, stdout, or an error message.
2. Never commit a credential: API key, password, private key, or token.
3. Never disable TLS verification or certificate checking.
"""

# The response shape. Note the schema carries no numeric bounds on confidence:
# structured outputs reject `minimum`/`maximum`, so the range is enforced in
# _coerce below instead.
VERDICT_SCHEMA = {
    "type": "object",
    "properties": {
        "decision": {
            "type": "string",
            "enum": list(DECISIONS),
            "description": (
                "'fix' = a real violation you are confident about and have "
                "rewritten. 'allow' = you are confident the change is fine. "
                "'escalate' = unsure or risky; a human must look."
            ),
        },
        "confidence": {
            "type": "number",
            "description": "0 to 1. How sure you are of this decision.",
        },
        "reason": {
            "type": "string",
            "description": (
                "Two or three sentences a reviewer can check, naming the file "
                "and what you saw. This is posted on the PR, so write it for "
                "the author, not for a log."
            ),
        },
        "fixed_code": {
            "type": "array",
            "description": (
                "Only for decision 'fix': the complete new contents of each "
                "file you changed. Empty array for 'allow' and 'escalate'."
            ),
            "items": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Repo-relative path, exactly as in the diff.",
                    },
                    "content": {
                        "type": "string",
                        "description": (
                            "The entire file after your fix, not a diff or a "
                            "snippet. It gets written to disk verbatim."
                        ),
                    },
                },
                "required": ["path", "content"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["decision", "confidence", "reason", "fixed_code"],
    "additionalProperties": False,
}

SYSTEM_PROMPT = """\
You are PR Guardian, reviewing one pull request diff against a codebase's rules.

Decide exactly one of:

- "fix": the diff really violates a rule, you are confident, and you can
  rewrite the offending file correctly. Return the complete fixed file(s).
- "allow": you are confident the diff is fine. Say why in one or two sentences.
- "escalate": you are unsure, the change is risky, or fixing it needs context
  you do not have. A human will look at it.

How to weigh it:

A wrong fix that gets merged is worse than an escalation. When genuinely torn,
escalate. But do not escalate to avoid thinking -- escalating something clearly
fine is also a failure, just a cheaper one.

Context decides, not keywords. The same literal string can be a violation in
one file and correct in another. A card number in a test fixture, a sample
value in documentation, or a constant in a test file is test data, not a leak.
The same number written to a log from request-handling code is a real
violation. Read what the code does with the value and where the file sits
before you judge it.

For "fix", change only what the violation requires. Do not reformat, rename,
refactor, add error handling, or improve unrelated lines -- the fix has to pass
the existing test suite, and a large diff is harder for a human to verify.
Preserve the file's existing style and imports.

Set confidence honestly. It gates whether anything merges without a human, so
a high number on a guess costs more than a low number on a good call.
"""


class _Client(Protocol):
    """The slice of anthropic.Anthropic the judge uses. Lets tests pass a fake."""

    @property
    def messages(self) -> Any: ...


@dataclass
class Verdict:
    """A decision about one PR, ready for db.write_verdict."""

    decision: str
    confidence: float
    reason: str
    fixed_code: list[dict] = field(default_factory=list)

    @property
    def is_fix(self) -> bool:
        return self.decision == "fix"

    @property
    def changed_paths(self) -> list[str]:
        return [f["path"] for f in self.fixed_code]


def load_rules(path: str | None = None) -> str:
    """Read the rules file, falling back to FALLBACK_RULES if it isn't there."""
    path = path or os.environ.get("PR_GUARDIAN_RULES") or DEFAULT_RULES_PATH
    try:
        with open(path, encoding="utf-8") as handle:
            text = handle.read().strip()
    except OSError:
        return FALLBACK_RULES
    return text or FALLBACK_RULES


def _format_memory(receipts: list[dict]) -> str:
    """Past decisions, as context for this one.

    Kept to decision + reason. The point is consistency with how we ruled
    before, and a long transcript would crowd out the diff.
    """
    if not receipts:
        return ""

    lines = []
    for receipt in receipts:
        reason = (receipt.get("reason") or "").strip().replace("\n", " ")
        if len(reason) > 300:
            reason = reason[:297] + "..."
        lines.append(
            f"- PR #{receipt.get('pr_number')}: "
            f"{receipt.get('decision')} -- {reason}"
        )

    return (
        "How this repo's earlier PRs were ruled on. Stay consistent with these "
        "unless this diff is materially different:\n" + "\n".join(lines)
    )


def build_user_content(diff: dict, rules: str, receipts: list[dict]) -> str:
    """Assemble the user turn: rules, memory, then the diff itself.

    The diff goes last so the model reads the standard it's judging against
    before the thing being judged.
    """
    files = diff.get("files") or []
    file_list = "\n".join(f"- {name}" for name in files) or "- (none listed)"

    parts = [
        f"The rules for this codebase:\n\n{rules}",
    ]

    memory = _format_memory(receipts)
    if memory:
        parts.append(memory)

    parts.append(
        f"Pull request #{diff.get('pr_number')} on {diff.get('repo')}, "
        f"branch {diff.get('branch')}.\n\n"
        f"Files changed:\n{file_list}\n\n"
        f"The diff:\n\n```diff\n{diff.get('diff_text', '')}\n```"
    )
    parts.append(
        "Judge this diff. Pay attention to which file each change is in before "
        "you decide."
    )

    return "\n\n---\n\n".join(parts)


def _coerce(payload: dict) -> Verdict:
    """Turn the model's JSON into a Verdict, enforcing what the schema can't.

    The schema pins decision to the enum and requires every key, so this is
    mostly about confidence: structured outputs don't support numeric bounds,
    so an out-of-range or non-numeric value has to be handled here rather than
    reaching VERDICTS and skewing the dashboard.
    """
    decision = payload.get("decision")
    if decision not in DECISIONS:
        # Shouldn't happen -- the enum is enforced server-side -- but a bad
        # value here would break /merge's gate, so fail safe.
        return Verdict(
            decision="escalate",
            confidence=0.0,
            reason=f"Judge returned an unknown decision {decision!r}; escalating.",
        )

    try:
        confidence = float(payload.get("confidence", 0.0))
    except (TypeError, ValueError):
        confidence = 0.0
    confidence = max(0.0, min(1.0, confidence))

    fixed_code = [
        {"path": item["path"], "content": item["content"]}
        for item in payload.get("fixed_code") or []
        if isinstance(item, dict) and "path" in item and "content" in item
    ]

    verdict = Verdict(
        decision=decision,
        confidence=confidence,
        reason=(payload.get("reason") or "").strip(),
        fixed_code=fixed_code,
    )

    # A "fix" with nothing to write can't be applied or tested, so it isn't a
    # fix. Escalate rather than hand /merge a verdict it would gate on an empty
    # change set.
    if verdict.is_fix and not fixed_code:
        return Verdict(
            decision="escalate",
            confidence=confidence,
            reason=(
                "Judge chose 'fix' but returned no file contents, so there is "
                "nothing to apply. Original reason: " + verdict.reason
            ),
        )

    # Files for a non-fix decision are meaningless to /merge; drop them so the
    # VERDICTS row says what it means.
    if not verdict.is_fix and fixed_code:
        verdict.fixed_code = []

    return verdict


def _extract_json(response: Any) -> dict:
    """Pull the JSON object out of the response's first text block.

    With output_config.format set, the text block is guaranteed to be valid
    JSON matching the schema -- unless the model refused or hit max_tokens,
    both of which are checked by the caller.
    """
    for block in response.content:
        if getattr(block, "type", None) == "text":
            return json.loads(block.text)
    raise ValueError("no text block in response")


def judge(
    diff: dict,
    client: _Client,
    rules: str | None = None,
    receipts: list[dict] | None = None,
    model: str | None = None,
) -> Verdict:
    """Judge one diff. Never raises: any failure becomes an escalation.

    That's deliberate. The engine runs unattended in a GitHub Action, and an
    exception there means no VERDICTS row at all, which /merge reads as "not
    judged yet" and the dashboard shows as a gap. An escalation is a real
    outcome a human can act on.
    """
    model = model or os.environ.get("PR_GUARDIAN_MODEL") or DEFAULT_MODEL
    rules = rules if rules is not None else load_rules()
    content = build_user_content(diff, rules, receipts or [])

    try:
        response = client.messages.create(
            model=model,
            max_tokens=MAX_TOKENS,
            system=SYSTEM_PROMPT,
            thinking={"type": "adaptive"},
            output_config={
                "effort": "high",
                "format": {"type": "json_schema", "schema": VERDICT_SCHEMA},
            },
            messages=[{"role": "user", "content": content}],
        )
    except Exception as exc:  # noqa: BLE001 - any API failure must escalate
        return Verdict(
            decision="escalate",
            confidence=0.0,
            reason=f"Could not reach the judge ({type(exc).__name__}: {exc}).",
        )

    stop_reason = getattr(response, "stop_reason", None)
    if stop_reason == "refusal":
        return Verdict(
            decision="escalate",
            confidence=0.0,
            reason="The judge declined to review this diff; a human should look.",
        )
    if stop_reason == "max_tokens":
        # The JSON is truncated and unparseable. Escalate rather than guess.
        return Verdict(
            decision="escalate",
            confidence=0.0,
            reason=(
                "The judge's response was cut off by the token limit, so the "
                "verdict is incomplete. A human should review this diff."
            ),
        )

    try:
        payload = _extract_json(response)
    except (ValueError, TypeError, AttributeError) as exc:
        return Verdict(
            decision="escalate",
            confidence=0.0,
            reason=f"Could not read the judge's response ({exc}).",
        )

    return _coerce(payload)
