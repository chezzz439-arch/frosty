#!/usr/bin/env python3
"""/trigger: read a PR's diff and hand it to Guardian.

Writes one PR_DIFFS row and two PR_EVENTS rows ("pr opened", "diff written").
The engine then logs "diff read" when it picks the row up.

Where the diff comes from (pick one):
  --from-event                    inside the GitHub Action ($GITHUB_EVENT_PATH + git)
  --repo OWNER/NAME --pr N        any real PR, from a laptop (gh CLI). Also the manual backup path.
  --planted violation|decoy|ambiguous [--pr N]
                                  build a planted PR's diff locally from demo-data/, no GitHub
                                  needed. Default PR numbers 101/102/103 match engine/seed_fake_rows.py.

Where it goes (--store, default auto):
  local      engine/db.py's local database (PR_GUARDIAN_DB), the store the engine reads today
  snowflake  PR_GUARDIAN.CORE.PR_DIFFS / PR_EVENTS (see sf.py for the connection)
  auto       every store that's available: local if engine/db.py exists, plus Snowflake if configured

--dry-run prints what would be written and touches nothing.
"""
from __future__ import annotations

import argparse
import importlib
import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parent
DEMO = ROOT / "demo-data"
PLANTED = {"violation": ("01-violation", 101), "decoy": ("02-decoy", 102), "ambiguous": ("03-ambiguous", 103)}
# ASSUMPTION: a diff past this size is noise for the judge; the cut is marked in the text.
MAX_DIFF_CHARS = int(os.environ.get("PR_GUARDIAN_MAX_DIFF_CHARS", "200000"))


def sh(*args, cwd=None) -> str:
    return subprocess.run(args, check=True, capture_output=True, text=True, cwd=cwd).stdout


# --- read the PR ---------------------------------------------------------------------------


def from_event() -> dict:
    event = json.loads(pathlib.Path(os.environ["GITHUB_EVENT_PATH"]).read_text())
    pr = event["pull_request"]
    # Three dots: only what the PR adds since it branched, same as GitHub's "Files changed".
    span = f"{pr['base']['sha']}...{pr['head']['sha']}"
    return {
        "pr_number": pr["number"],
        "repo": event["repository"]["full_name"],
        "branch": pr["head"]["ref"],
        "title": pr.get("title") or "",
        "files": [f for f in sh("git", "diff", "--name-only", span).splitlines() if f],
        "diff_text": sh("git", "diff", "--no-color", span),
    }


def from_gh(repo: str, number: int) -> dict:
    meta = json.loads(sh("gh", "pr", "view", str(number), "--repo", repo,
                         "--json", "number,title,headRefName,files"))
    return {
        "pr_number": meta["number"],
        "repo": repo,
        "branch": meta["headRefName"],
        "title": meta.get("title") or "",
        "files": [f["path"] for f in meta.get("files") or []],
        "diff_text": sh("gh", "pr", "diff", str(number), "--repo", repo, "--color", "never"),
    }


def planted_meta(folder: pathlib.Path) -> dict:
    head = folder.joinpath("pr.md").read_text().split("\n---\n", 1)[0]
    return dict(line.split(": ", 1) for line in head.splitlines() if ": " in line)


def from_planted(name: str, number: int | None, repo: str) -> dict:
    folder_name, default_number = PLANTED[name]
    folder = DEMO / "planted-prs" / folder_name
    meta = planted_meta(folder)
    with tempfile.TemporaryDirectory(prefix="planted-") as tmp:
        work = pathlib.Path(tmp) / "shop"
        shutil.copytree(DEMO / "shop", work)
        git = ["git", "-c", "user.name=guardian", "-c", "user.email=guardian@example.com"]
        sh("git", "init", "-q", cwd=work)
        sh(*git, "add", "-A", cwd=work)
        sh(*git, "commit", "-q", "-m", "base", cwd=work)
        shutil.copytree(folder / "files", work, dirs_exist_ok=True)
        sh("git", "add", "-A", cwd=work)
        files = [f for f in sh("git", "diff", "--cached", "--name-only", cwd=work).splitlines() if f]
        diff = sh("git", "diff", "--cached", "--no-color", cwd=work)
    return {
        "pr_number": number or default_number,
        "repo": repo,
        "branch": meta.get("branch", f"planted/{name}"),
        "title": meta.get("title", name),
        "files": files,
        "diff_text": diff,
    }


def summarize(pr: dict) -> str:
    lines = pr["diff_text"].splitlines()
    adds = sum(1 for l in lines if l.startswith("+") and not l.startswith("+++"))
    dels = sum(1 for l in lines if l.startswith("-") and not l.startswith("---"))
    return f"{len(pr['files'])} file(s), +{adds}/-{dels}: {', '.join(pr['files'][:5])}"


# --- stores --------------------------------------------------------------------------------


class LocalStore:
    """engine/db.py itself, so the row is exactly what the engine expects to read."""

    name = "local"

    def __init__(self, engine_dir: pathlib.Path):
        sys.path.insert(0, str(engine_dir))
        self.db = importlib.import_module("db")

    def write(self, pr: dict, summary: str):
        self.db.log_event(pr["pr_number"], "pr opened", "ok", pr["title"][:500])
        self.db.insert_diff(pr_number=pr["pr_number"], repo=pr["repo"], branch=pr["branch"],
                            files=pr["files"], diff_text=pr["diff_text"])
        self.db.log_event(pr["pr_number"], "diff written", "ok", summary[:500])

    def fail(self, pr_number: int, detail: str):
        self.db.log_event(pr_number, "diff written", "failed", detail[:500])


class SnowflakeStore:
    """PR_GUARDIAN.CORE, column lists exactly as sql/setup.sql; created_at/ts use their defaults."""

    name = "snowflake"

    def __init__(self, connection_name: str | None):
        sys.path.insert(0, str(HERE))
        import sf

        self.conn = sf.connect(connection_name)

    def _event(self, cur, pr_number, step, status, detail):
        cur.execute("INSERT INTO PR_EVENTS (pr_number, step, status, detail) SELECT %s, %s, %s, %s",
                    (pr_number, step, status, detail[:500]))

    def write(self, pr: dict, summary: str):
        cur = self.conn.cursor()
        self._event(cur, pr["pr_number"], "pr opened", "ok", pr["title"])
        # PARSE_JSON because files is ARRAY; INSERT ... VALUES can't take a function call.
        cur.execute(
            "INSERT INTO PR_DIFFS (pr_number, repo, branch, files, diff_text) "
            "SELECT %s, %s, %s, PARSE_JSON(%s), %s",
            (pr["pr_number"], pr["repo"], pr["branch"], json.dumps(pr["files"]), pr["diff_text"]),
        )
        self._event(cur, pr["pr_number"], "diff written", "ok", summary)
        self.conn.commit()

    def fail(self, pr_number: int, detail: str):
        self._event(self.conn.cursor(), pr_number, "diff written", "failed", detail)
        self.conn.commit()


def open_stores(choice: str, engine_dir: pathlib.Path, connection: str | None) -> list:
    sys.path.insert(0, str(HERE))
    import sf

    want_local = choice in ("local", "both") or (choice == "auto" and (engine_dir / "db.py").exists())
    want_sf = choice in ("snowflake", "both") or (choice == "auto" and sf.configured(connection))
    if choice in ("local", "both") and not (engine_dir / "db.py").exists():
        raise SystemExit(f"--store {choice}: no db.py in {engine_dir} (pass --engine-dir)")
    stores = []
    if want_local:
        stores.append(LocalStore(engine_dir))
    if want_sf:
        stores.append(SnowflakeStore(connection))
    if not stores:
        raise SystemExit("Nowhere to write: no engine/db.py found and Snowflake isn't configured. "
                         "Use --dry-run, --engine-dir, --connection or SNOWFLAKE_* env vars.")
    return stores


# --- main ----------------------------------------------------------------------------------


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--from-event", action="store_true")
    src.add_argument("--repo")
    src.add_argument("--planted", choices=sorted(PLANTED))
    ap.add_argument("--pr", type=int)
    ap.add_argument("--planted-repo", default=os.environ.get("PR_REPO", "local/demo-shop"),
                    help="repo name recorded for --planted rows")
    ap.add_argument("--store", choices=("auto", "local", "snowflake", "both"), default="auto")
    ap.add_argument("--engine-dir", type=pathlib.Path,
                    default=pathlib.Path(os.environ.get("PR_GUARDIAN_ENGINE_DIR", ROOT / "engine")))
    ap.add_argument("--connection", help="snow CLI connection name (laptop)")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args(argv)

    if a.from_event:
        pr = from_event()
    elif a.repo:
        if not a.pr:
            ap.error("--repo needs --pr N")
        pr = from_gh(a.repo, a.pr)
    else:
        pr = from_planted(a.planted, a.pr, a.planted_repo)

    if len(pr["diff_text"]) > MAX_DIFF_CHARS:
        cut = len(pr["diff_text"]) - MAX_DIFF_CHARS
        pr["diff_text"] = pr["diff_text"][:MAX_DIFF_CHARS] + f"\n... [diff truncated by /trigger: {cut} more chars]\n"
    summary = summarize(pr)

    if a.dry_run:
        print(json.dumps({k: v for k, v in pr.items() if k != "diff_text"}, indent=2))
        print(f"--- diff_text ({len(pr['diff_text'])} chars) ---\n{pr['diff_text']}")
        print(f"--- PR_EVENTS ---\npr opened   ok  {pr['title']}\ndiff written ok  {summary}")
        return 0

    stores = open_stores(a.store, a.engine_dir, a.connection)
    failed = False
    for store in stores:
        try:
            store.write(pr, summary)
            print(f"[{store.name}] PR #{pr['pr_number']} written: {summary}")
        except Exception as exc:  # one store failing must not stop the other
            failed = True
            print(f"[{store.name}] FAILED: {type(exc).__name__}: {exc}", file=sys.stderr)
            try:
                store.fail(pr["pr_number"], f"{type(exc).__name__}: {exc}")
            except Exception:
                pass
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
