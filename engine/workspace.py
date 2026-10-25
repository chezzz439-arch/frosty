"""The filesystem half of the engine: read the branch, sandbox it, test it.

Nothing here talks to Claude or to the four tables. It exists so the judge can
be given whole files instead of a diff hunk, and so a proposed fix can be tried
somewhere that isn't the real checkout.

The repo root is the checked-out PR branch. In the GitHub Action that's the
working directory, because the Action checks the branch out before running the
engine. Locally, point PR_GUARDIAN_REPO at the demo tree that
engine/seed_fake_rows.py writes.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile

# ASSUMPTION: the Action runs the engine from the root of the checked-out PR
# branch. Override with PR_GUARDIAN_REPO.
def repo_root() -> str:
    return os.path.abspath(os.environ.get("PR_GUARDIAN_REPO") or os.getcwd())


# ASSUMPTION: /demo-data's suite is runnable with stdlib unittest discovery.
# That folder is Erik's and its suite isn't written yet, so this is a
# placeholder that works on the demo tree. Override with PR_GUARDIAN_TEST_CMD
# (a shell-style string, e.g. "python -m pytest -q").
DEFAULT_TEST_COMMAND = [sys.executable, "-m", "unittest", "discover", "-s", "tests", "-t", "."]

# A hung test suite must not hang the Action.
DEFAULT_TEST_TIMEOUT = 300

# Enough of a failure for the judge to act on, short enough not to crowd the
# diff and the file contents out of the prompt.
MAX_TEST_OUTPUT = 6000


class UnsafePath(Exception):
    """A path from the model pointed outside the sandbox."""


def _resolve_inside(root: str, relative_path: str) -> str:
    """Resolve a repo-relative path, refusing anything that escapes the root.

    `fixed_code` paths come from the model, which is reading an
    attacker-controlled diff. A path like ../../.github/workflows/ci.yml or an
    absolute path would otherwise be written wherever it pointed. The sandbox
    is a temp copy, but the same helper reads the real checkout, so this is
    enforced in one place for both.
    """
    root = os.path.realpath(root)
    candidate = os.path.realpath(os.path.join(root, relative_path))
    if candidate != root and not candidate.startswith(root + os.sep):
        raise UnsafePath(f"{relative_path!r} resolves outside the repository")
    return candidate


def read_sources(root: str, paths: list[str]) -> list[dict]:
    """The full current contents of each file the PR touches.

    A file that can't be read gets content None rather than being dropped: the
    judge is told the file wasn't readable, which is honest, instead of being
    left to assume it was empty or never existed.
    """
    sources = []
    for path in paths:
        try:
            with open(_resolve_inside(root, path), encoding="utf-8") as handle:
                content = handle.read()
        except (OSError, UnsafePath, UnicodeDecodeError):
            content = None
        sources.append({"path": path, "content": content})
    return sources


def copy_repo(root: str) -> str:
    """Copy the checkout to a temp directory. Caller removes it.

    CLAUDE.md calls for a sandbox: the fix is applied and tested somewhere
    disposable, and the real checkout is never written to. Copying the tree is
    enough for that and needs no git state.
    """
    destination = tempfile.mkdtemp(prefix="pr-guardian-sandbox-")
    sandbox = os.path.join(destination, "repo")
    shutil.copytree(
        root,
        sandbox,
        # .git is large and irrelevant to running tests; __pycache__ would
        # shadow the rewritten source with stale bytecode.
        ignore=shutil.ignore_patterns(".git", "__pycache__", "*.pyc", ".venv"),
        symlinks=True,
    )
    return sandbox


def apply_fix(sandbox: str, fixed_code: list[dict]) -> list[str]:
    """Write the model's files into the sandbox. Returns the paths written.

    Raises UnsafePath if a path escapes the sandbox -- a fix we can't place
    safely is not a fix, and the caller turns that into an escalation.
    """
    written = []
    for item in fixed_code:
        target = _resolve_inside(sandbox, item["path"])
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with open(target, "w", encoding="utf-8") as handle:
            handle.write(item["content"])
        written.append(item["path"])
    return written


def test_command() -> list[str]:
    override = os.environ.get("PR_GUARDIAN_TEST_CMD")
    if override:
        import shlex

        return shlex.split(override)
    return list(DEFAULT_TEST_COMMAND)


def run_tests(sandbox: str, timeout: float | None = None) -> tuple[bool, str]:
    """Run the suite in the sandbox. Returns (passed, output).

    A timeout or a missing test runner counts as a failure, not an exception:
    the caller is mid-retry-loop and needs something to feed back to the judge.
    """
    timeout = timeout or float(
        os.environ.get("PR_GUARDIAN_TEST_TIMEOUT") or DEFAULT_TEST_TIMEOUT
    )
    argv = test_command()

    try:
        completed = subprocess.run(
            argv,
            cwd=sandbox,
            capture_output=True,
            text=True,
            timeout=timeout,
            # Stop the suite importing the engine's own modules by accident.
            env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
        )
    except subprocess.TimeoutExpired:
        return False, f"The test suite did not finish within {timeout:.0f}s."
    except FileNotFoundError:
        return False, f"Could not run the test suite: {argv[0]!r} not found."

    output = ((completed.stdout or "") + (completed.stderr or "")).strip()
    if len(output) > MAX_TEST_OUTPUT:
        # Keep the tail: the failure summary is at the end of a test run.
        output = "...(truncated)\n" + output[-MAX_TEST_OUTPUT:]

    return completed.returncode == 0, output
