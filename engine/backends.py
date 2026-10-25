"""Two ways to ask Claude for a verdict, behind one small interface.

The judge doesn't care which one it's talking to. A backend takes a system
prompt, a user turn and a JSON Schema, and returns the decoded JSON object --
or raises BackendError, which the judge turns into an escalation.

    api         anthropic SDK, needs ANTHROPIC_API_KEY
    claude-cli  shells out to `claude -p`, uses the Claude Code subscription
                login (or CLAUDE_CODE_OAUTH_TOKEN in CI)

Which one runs: PR_GUARDIAN_BACKEND if set, otherwise "api" when
ANTHROPIC_API_KEY is in the environment and "claude-cli" when it isn't.

Every CLI flag used below was checked against `claude --help` (v2.1.168) and
https://code.claude.com/docs/en/cli-reference before being written here.
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
from typing import Any, Protocol

API = "api"
CLAUDE_CLI = "claude-cli"
BACKEND_NAMES = (API, CLAUDE_CLI)

# Non-streaming request, so keep max_tokens under the SDK's HTTP timeout guard.
# Rewritten files are whole-file contents, hence not small. CLI-only runs
# ignore this -- `claude -p` has no equivalent flag.
MAX_TOKENS = 16000

# Both backends ask for the same thinking depth, so a verdict doesn't change
# character depending on which one the Action happened to pick. "high" is the
# documented default for both; we pass it explicitly to pin it.
EFFORT = "high"

# ASSUMPTION: a judge call finishes well inside five minutes. If it doesn't,
# something is wrong and escalating beats blocking the Action. Override with
# PR_GUARDIAN_CLI_TIMEOUT (seconds).
DEFAULT_CLI_TIMEOUT = 300

# The CLI needs a prompt argument even when the real content arrives on stdin.
CLI_PROMPT = (
    "Judge the pull request given on standard input, following your "
    "instructions exactly."
)


class BackendError(Exception):
    """The backend could not produce a verdict.

    The message is written to be readable on a PR, because the judge puts it
    straight into the escalation reason a human will read.
    """


class Backend(Protocol):
    def complete(self, system: str, user: str, schema: dict, model: str) -> dict: ...


# --- anthropic SDK -----------------------------------------------------------


class ApiBackend:
    """The Messages API, with the response constrained to the schema.

    Structured outputs (`output_config.format`) rather than a tool call: one
    round trip, and no chance of a text-only reply we have to re-prompt for. A
    tool call would also work but needs the tool_use block extracted and can be
    skipped by the model.
    https://platform.claude.com/docs/en/build-with-claude/structured-outputs
    """

    name = API

    def __init__(self, client: Any):
        # Injected so the judge is testable without a key -- see test_judge.py.
        self.client = client

    def complete(self, system: str, user: str, schema: dict, model: str) -> dict:
        # Transport failures propagate: the judge formats them as escalations.
        response = self.client.messages.create(
            model=model,
            max_tokens=MAX_TOKENS,
            system=system,
            thinking={"type": "adaptive"},
            output_config={
                "effort": EFFORT,
                "format": {"type": "json_schema", "schema": schema},
            },
            messages=[{"role": "user", "content": user}],
        )

        stop_reason = getattr(response, "stop_reason", None)
        if stop_reason == "refusal":
            raise BackendError(
                "The judge declined to review this diff; a human should look."
            )
        if stop_reason == "max_tokens":
            # The JSON is truncated and unparseable. Escalate rather than guess.
            raise BackendError(
                "The judge's response was cut off by the token limit, so the "
                "verdict is incomplete. A human should review this diff."
            )

        try:
            return _first_text_block(response)
        except (ValueError, TypeError, AttributeError) as exc:
            raise BackendError(f"Could not read the judge's response ({exc}).") from exc


def _first_text_block(response: Any) -> dict:
    """Pull the JSON object out of the response's first text block.

    With output_config.format set, that block is guaranteed to be valid JSON
    matching the schema -- unless the model refused or hit max_tokens, both of
    which are checked above.
    """
    for block in response.content:
        if getattr(block, "type", None) == "text":
            return json.loads(block.text)
    raise ValueError("no text block in response")


# --- claude -p ---------------------------------------------------------------


class CliBackend:
    """`claude -p`, for when there is no API key.

    This authenticates with the Claude Code login already on the machine, so a
    developer with a Pro/Max subscription can run the engine without a Console
    key. In the GitHub Action, the same login is supplied as the
    CLAUDE_CODE_OAUTH_TOKEN secret, generated once with `claude setup-token`.

    Three things about the flags below are load-bearing:

    `--json-schema` makes the CLI return a validated object in the response's
    `structured_output` field, which is the same guarantee the API backend gets
    from output_config.format. The field is absent if the run produced no valid
    structured output, so we check for it rather than assume it.

    `--bare` is deliberately NOT used. It is the documented recommendation for
    scripted calls, but it never reads OAuth credentials or
    CLAUDE_CODE_OAUTH_TOKEN -- it only accepts ANTHROPIC_API_KEY. Using it here
    would break the one thing this backend exists for.

    Because we can't use --bare, the run would otherwise pick up whatever is in
    the working directory: the repo's own CLAUDE.md, its hooks, its .mcp.json.
    A PR Guardian judging a PR must not be steered by files from the branch it
    is judging, so the subprocess runs in an empty temp directory with
    --strict-mcp-config and --disable-slash-commands. That isolation is a
    security property, not a tidiness one.
    """

    name = CLAUDE_CLI

    def __init__(self, binary: str | None = None, timeout: float | None = None):
        self.binary = binary or os.environ.get("PR_GUARDIAN_CLAUDE_BIN") or "claude"
        self.timeout = timeout or float(
            os.environ.get("PR_GUARDIAN_CLI_TIMEOUT") or DEFAULT_CLI_TIMEOUT
        )

    def argv(self, system: str, schema: dict, model: str) -> list[str]:
        return [
            self.binary,
            "-p",
            CLI_PROMPT,
            "--output-format", "json",
            "--json-schema", json.dumps(schema),
            "--system-prompt", system,
            "--model", model,
            "--effort", EFFORT,
            # The judge reads a diff we hand it and answers. It has no reason to
            # touch the filesystem or run commands, and a diff is attacker-
            # controlled text, so give it nothing to be talked into using.
            "--tools", "",
            # Belt and braces: with no tools nothing can prompt, but an
            # unattended Action must never block waiting for a human.
            "--permission-mode", "dontAsk",
            "--strict-mcp-config",
            "--disable-slash-commands",
            # One verdict per PR; don't litter the runner with session files.
            "--no-session-persistence",
            # Measured, not guessed: the decoy PR takes 2 turns, the violation
            # takes 4 (structured output costs a turn of its own, on top of the
            # reasoning turns). 3 was too few and silently turned a real verdict
            # into an error_max_turns escalation. 12 leaves headroom for a
            # harder diff while still bounding a runaway.
            "--max-turns", "12",
        ]

    def complete(self, system: str, user: str, schema: dict, model: str) -> dict:
        argv = self.argv(system, schema, model)

        # The diff goes on stdin, not in argv: a large PR would blow past the
        # operating system's argument-length limit.
        with tempfile.TemporaryDirectory(prefix="pr-guardian-cli-") as neutral_cwd:
            try:
                completed = subprocess.run(
                    argv,
                    input=user,
                    capture_output=True,
                    text=True,
                    timeout=self.timeout,
                    cwd=neutral_cwd,
                )
            except FileNotFoundError as exc:
                raise BackendError(
                    f"The Claude Code CLI ({self.binary}) isn't on PATH, so the "
                    "judge could not run. Install it or set ANTHROPIC_API_KEY "
                    "to use the api backend."
                ) from exc
            except subprocess.TimeoutExpired as exc:
                raise BackendError(
                    f"The judge did not answer within {self.timeout:.0f}s."
                ) from exc

        return _parse_cli_output(completed)


def _parse_cli_output(completed: subprocess.CompletedProcess) -> dict:
    """Read the CLI's result envelope, or say precisely what went wrong.

    The CLI reports in-run failures -- a missing login above all -- as a normal
    JSON result with is_error true, not as a non-zero exit, so the envelope has
    to be inspected rather than trusted.
    """
    stdout = (completed.stdout or "").strip()
    stderr = (completed.stderr or "").strip()

    if not stdout:
        raise BackendError(
            f"The Claude Code CLI exited {completed.returncode} with no output"
            + (f": {_clip(stderr)}" if stderr else ".")
        )

    try:
        envelope = json.loads(stdout)
    except ValueError as exc:
        raise BackendError(
            f"Could not read the Claude Code CLI's output ({exc}): {_clip(stdout)}"
        ) from exc

    if not isinstance(envelope, dict):
        raise BackendError("The Claude Code CLI returned JSON that wasn't an object.")

    if envelope.get("is_error"):
        # subtype says what kind, e.g. error_max_turns; result carries the text
        # of an auth failure.
        detail = envelope.get("subtype") or "unknown error"
        text = _clip(str(envelope.get("result") or "")) or _clip(stderr)
        raise BackendError(
            f"The Claude Code CLI reported {detail}"
            + (f": {text}" if text else ".")
            + " Check that `claude` is logged in, or that "
            "CLAUDE_CODE_OAUTH_TOKEN is set."
        )

    payload = envelope.get("structured_output")
    if payload is None:
        # Documented: the field is absent when the run produced no valid
        # structured output, e.g. because Claude declined the request.
        raise BackendError(
            "The Claude Code CLI returned no structured verdict"
            + (f": {_clip(str(envelope.get('result') or ''))}" if envelope.get("result") else ".")
        )
    if not isinstance(payload, dict):
        raise BackendError("The Claude Code CLI's structured output wasn't an object.")

    return payload


def _clip(text: str, limit: int = 300) -> str:
    """Keep an error message short enough to read on a PR comment."""
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 3] + "..."


# --- choosing ----------------------------------------------------------------


def select_backend_name(name: str | None = None) -> str:
    """Which backend to use, given the environment.

    Explicit PR_GUARDIAN_BACKEND wins. Otherwise a key means the API, and no
    key means the CLI -- so a developer with only a Claude subscription gets a
    working engine without setting anything.
    """
    chosen = name or os.environ.get("PR_GUARDIAN_BACKEND") or ""
    chosen = chosen.strip()

    if chosen:
        if chosen not in BACKEND_NAMES:
            raise ValueError(
                f"PR_GUARDIAN_BACKEND must be one of {BACKEND_NAMES}, got {chosen!r}"
            )
        return chosen

    return API if os.environ.get("ANTHROPIC_API_KEY") else CLAUDE_CLI


def build_backend(name: str | None = None) -> Backend:
    """Build the chosen backend, or raise RuntimeError saying how to fix it.

    Raises rather than escalating: a missing SDK or a missing login is a setup
    problem, not a verdict about the PR. run.py builds the backend before it
    logs anything, so a setup failure leaves no half-finished trail on the
    dashboard.
    """
    chosen = select_backend_name(name)

    if chosen == CLAUDE_CLI:
        return CliBackend()

    try:
        import anthropic
    except ImportError as exc:
        raise RuntimeError(
            "The api backend needs the anthropic SDK. Either run\n"
            "    pip install -r engine/requirements.txt\n"
            "or use the Claude Code CLI instead:\n"
            "    PR_GUARDIAN_BACKEND=claude-cli python engine/run.py <pr_number>"
        ) from exc

    if not os.environ.get("ANTHROPIC_API_KEY"):
        raise RuntimeError(
            "The api backend needs ANTHROPIC_API_KEY. Unset PR_GUARDIAN_BACKEND "
            "to fall back to the Claude Code CLI."
        )

    # Reads ANTHROPIC_API_KEY from the environment.
    return ApiBackend(anthropic.Anthropic())
