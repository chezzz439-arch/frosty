"""PR Guardian dashboard. Demo mode needs only Streamlit and pandas."""
import html
import json
import os
import re
import time
import uuid
from datetime import datetime, timedelta, timezone

SCENARIOS = {
    "Fix + retry": [
        ("diff_read", "passed", "Read a synthetic application logging change."),
        ("rules_checked", "passed", "Rule LOG-001: never write card numbers, passwords, or tokens to logs."),
        ("verdict", "passed", "FIX: application code writes a card number to its logs."),
        ("sandbox_ready", "passed", "SIMULATED temporary repository sandbox prepared on the runner."),
        ("fix_written", "passed", "SIMULATED patch removes the raw card number from the log message."),
        ("tests_failed", "failed", "Attempt 1: a debug log still contains the card number. Retry required."),
        ("fix_retried", "passed", "Attempt 2: redact the remaining debug log and preserve application behavior."),
        ("tests_passed", "passed", "SIMULATED no-card-in-logs and application regression checks pass."),
        ("merge_simulated", "passed", "SIMULATED merge only. No GitHub request was made."),
        ("receipt_written", "passed", "Synthetic decision receipt saved to this session."),
    ],
    "Allow decoy": [
        ("diff_read", "passed", "Read a synthetic fixture change."),
        ("rules_checked", "passed", "Fixture context and fake-data provenance checked."),
        ("verdict", "passed", "ALLOW: fake test card data in an isolated fixture."),
        ("receipt_written", "passed", "Allowed for demo purposes; no merge was performed."),
    ],
    "Escalate": [
        ("diff_read", "passed", "Read a synthetic change with unclear logging requirements."),
        ("rules_checked", "passed", "Business purpose and authorized audience are unclear."),
        ("verdict", "warning", "ESCALATE: an authorized human must review this change."),
        ("escalated", "warning", "Waiting for human review. No fix or merge is attempted."),
        ("receipt_written", "passed", "Escalation receipt recorded."),
    ],
    "Failed run": [
        ("diff_read", "passed", "Read a synthetic migration."),
        ("sandbox_failed", "failed", "SIMULATED runner error: temporary repository setup failed."),
        ("run_failed", "failed", "Stopped safely. No merge; inspect the error before retrying."),
    ],
}
OUTCOMES = {
    "Fix + retry": ("fix", "Remove card data from application logs; the second simulated test attempt passed.", True),
    "Allow decoy": ("allow", "Synthetic card data is confined to an explicitly identified test fixture; simulated merge as-is.", True),
    "Escalate": ("escalate", "The intended audience and business justification need human review.", False),
}


def utcnow():
    return datetime.now(timezone.utc)


def iso(value):
    return value.isoformat(timespec="seconds")


def new_demo_run(repo, pr, scenario, now):
    return {"run_id": str(uuid.uuid4()), "repo": repo, "pr_number": int(pr),
            "head_sha": "demo-" + uuid.uuid4().hex[:12], "scenario": scenario,
            "created_at": iso(now), "next_event": 0, "finished": False,
            "state": "queued", "is_demo": True, "decision": OUTCOMES.get(scenario, ("escalate", "", False))[0],
            "confidence": {"Fix + retry": 0.98, "Allow decoy": 0.99, "Escalate": 0.45, "Failed run": None}[scenario],
            "reason": OUTCOMES.get(scenario, ("", "Runner failed before a decision.", False))[1]}


def add_event(data, run, step, status, detail, stamp):
    data["events"].append({"event_id": str(uuid.uuid4()), "run_id": run["run_id"],
                           "repo": run["repo"], "pr_number": run["pr_number"],
                           "head_sha": run["head_sha"], "step": step, "status": status,
                           "detail": detail, "ts": iso(stamp), "is_demo": True})


def finish_receipt(data, run, stamp):
    outcome = OUTCOMES.get(run["scenario"])
    if outcome and not any(r["run_id"] == run["run_id"] for r in data["receipts"]):
        decision, reason, merged = outcome
        data["receipts"].append({"receipt_id": str(uuid.uuid4()), "run_id": run["run_id"],
                                 "repo": run["repo"], "pr_number": run["pr_number"],
                                 "head_sha": run["head_sha"], "decision": decision,
                                 "reason": reason, "merged": merged, "receipt_url": "",
                                 "created_at": iso(stamp), "is_demo": True})


def tick_demo(data, now, seconds=3):
    """Advance by wall time, never by render count; reruns cannot duplicate steps."""
    for run in data["runs"]:
        if run["finished"]:
            continue
        start = datetime.fromisoformat(run["created_at"])
        elapsed = max(0, (now - start).total_seconds())
        steps = SCENARIOS[run["scenario"]]
        due = min(len(steps), int(elapsed // seconds))
        while run["next_event"] < due:
            index = run["next_event"]
            step, status, detail = steps[index]
            stamp = start + timedelta(seconds=(index + 1) * seconds)
            add_event(data, run, step, status, detail, stamp)
            run["next_event"] += 1
        run["state"] = "running" if due else "queued"
        if due == len(steps):
            run["finished"] = True
            run["state"] = "failed" if run["scenario"] == "Failed run" else "completed"
            finish_receipt(data, run, start + timedelta(seconds=len(steps) * seconds))
        for request in data["requests"]:
            if request["run_id"] == run["run_id"]:
                request["status"] = run["state"]


def seed_demo(now):
    data = {"runs": [], "events": [], "receipts": [], "requests": []}
    for n, scenario in enumerate(["Fix + retry", "Allow decoy", "Escalate"]):
        data["runs"].append(new_demo_run("demo/pr-guardian", 101 + n, scenario,
                                          now - timedelta(minutes=12 - n * 3)))
    tick_demo(data, now)
    return data


def queue_demo(data, repo, pr, scenario, now):
    if scenario not in SCENARIOS:
        raise ValueError("Unknown demo scenario")
    validate_pr(repo, pr)
    if any(r["repo"] == repo and r["pr_number"] == int(pr) and not r["finished"]
           for r in data["runs"]):
        raise ValueError("This PR already has a queued or running demo request.")
    run = new_demo_run(repo, pr, scenario, now)
    data["runs"].append(run)
    request = {"request_id": str(uuid.uuid4()), "run_id": run["run_id"], "repo": repo,
               "pr_number": int(pr), "status": "queued", "created_at": iso(now),
               "is_demo": True}
    data["requests"].append(request)
    add_event(data, run, "manual_request", "queued", "Synthetic backup request queued by the viewer.", now)
    return request


def validate_pr(repo, pr):
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo) or repo.endswith("."):
        raise ValueError("Use a repository in owner/repository format.")
    if isinstance(pr, bool) or int(pr) != pr or not 1 <= int(pr) <= 2147483647:
        raise ValueError("PR number must be a positive whole number.")


def qualified_prefix(value):
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*\.[A-Za-z_][A-Za-z0-9_]*", value):
        raise ValueError("Use an unquoted DATABASE.SCHEMA name.")
    return value.upper()


def safe_receipt_url(value):
    # Never make arbitrary database-supplied URLs into clickable links.
    value = str(value or "")
    return value if re.fullmatch(r"https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/pull/[0-9]+(?:#issuecomment-[0-9]+)?", value) else None


def filter_receipts(rows, term="", decisions=None):
    term = term.strip().casefold()
    return [r for r in rows if (not decisions or r["decision"] in decisions)
            and (not term or term in " ".join(str(r.get(k, "")) for k in
                 ["repo", "pr_number", "run_id", "decision", "reason"]).casefold())]


def get_session(st):
    try:
        from snowflake.snowpark.context import get_active_session
        return get_active_session()
    except Exception:
        if hasattr(st, "connection"):
            return st.connection("snowflake").session()
        raise RuntimeError("Open this app in Snowflake or configure a local Snowflake connection.")


def read_team(st, prefix, repo="chezzz439-arch/frosty"):
    """Read the updated guide's existing columns without changing shared tables.

    ASSUMPTION: these tables cover one repository and one current run per PR.
    The guide has no run/commit IDs, so all events for a PR are grouped together.
    """
    session = get_session(st)
    def rows(table, cols, order):
        result = session.sql(f"SELECT {cols} FROM {prefix}.{table} ORDER BY {order} DESC LIMIT 1000").collect()
        return [{str(k).lower(): v for k, v in row.as_dict().items()} for row in result]
    events = rows("PR_EVENTS", "PR_NUMBER,STEP,STATUS,DETAIL,TS", "TS")
    receipts = rows("RECEIPTS", "PR_NUMBER,DECISION,REASON,MERGED,RECEIPT_URL,CREATED_AT", "CREATED_AT")
    try:
        verdicts = rows("VERDICTS", "PR_NUMBER,DECISION,CONFIDENCE,REASON,CREATED_AT", "CREATED_AT")
    except Exception:
        verdicts = []
    latest = {}
    for v in verdicts:
        latest.setdefault(int(v["pr_number"]), v)
    complete = {int(r["pr_number"]) for r in receipts}
    seen = {}
    for e in reversed(events):
        pr = int(e["pr_number"])
        rid = f"pr-{pr}"
        e.update(run_id=rid, repo=repo, head_sha="Not supplied by team tables")
        # PR_EVENTS.STATUS is ok / failed / running -- nothing else is written.
        # Failure is carried by STATUS, not by the step name: the engine logs
        # ('diff read', 'failed'), so matching on step missed every real failure.
        status = str(e.get("status") or "running").lower()
        state = ("completed" if pr in complete else "failed" if status == "failed"
                 else "running")
        v = latest.get(pr, {})
        seen[rid] = {"run_id": rid, "repo": repo, "pr_number": pr,
                     "head_sha": "Not supplied by team tables", "created_at": str(e["ts"]),
                     "state": state, "decision": v.get("decision"),
                     "confidence": v.get("confidence"), "reason": v.get("reason")}
    for r in receipts:
        pr = int(r["pr_number"])
        r.update(run_id=f"pr-{pr}", repo=repo, head_sha="Not supplied by team tables")
        if r["run_id"] not in seen:
            seen[r["run_id"]] = dict(r, state="completed")
        elif not seen[r["run_id"]].get("decision"):
            seen[r["run_id"]].update(decision=r["decision"], reason=r["reason"])
    return {"events": events, "receipts": receipts, "runs": list(seen.values()), "requests": []}


def manual_event_sql(prefix):
    # STATUS is 'running': the four-table contract allows exactly ok / failed /
    # running, and a requested-but-unfinished run is 'running'. It was 'waiting',
    # which no other component writes or reads.
    return (f"INSERT INTO {qualified_prefix(prefix)}.PR_EVENTS (PR_NUMBER,STEP,STATUS,DETAIL,TS) "
            "SELECT ?,'manual run requested','running',?,CURRENT_TIMESTAMP()")


def main():
    import pandas as pd
    import streamlit as st

    st.set_page_config(page_title="PR Guardian | Control room", page_icon="🛡️", layout="wide")
    st.markdown("""<style>
    .block-container {padding-top:2rem; max-width:1450px}
    h1 {letter-spacing:-1.5px} h2,h3 {letter-spacing:-.5px}
    .eyebrow {color:#277b92;font-size:12px;letter-spacing:2px;font-weight:700}
    .event {border-left:3px solid #16856a;padding:10px 18px;margin:0 0 10px;background:#f3f7f9;border-radius:0 8px 8px 0;color:#172b3a}
    .event.failed {border-color:#d75555}.event.warning {border-color:#c78d18}.event.queued {border-color:#8296a5}
    .event small {color:#526b7b}.event p {margin:5px 0;font-size:14px}
    .badge {display:inline-block;background:#e8f3f2;color:#176653;border-radius:20px;padding:3px 10px;font-size:11px;font-weight:700}
    </style>""", unsafe_allow_html=True)
    st.markdown('<div class="eyebrow">PR GUARDIAN / CONTROL ROOM</div>', unsafe_allow_html=True)
    st.title("Every change. Every decision.")
    st.caption("Follow the review pipeline, inspect the evidence, and recover a missed trigger.")

    mode = st.sidebar.radio("Data source", ["Demo data", "Team Snowflake tables"])
    page = st.sidebar.radio("Workspace", ["Live feed", "Receipt history", "Manual backup"])
    auto = st.sidebar.checkbox("Live updates every 3 seconds", value=True)
    st.sidebar.button("Refresh now")
    if "guardian_demo" not in st.session_state:
        st.session_state["guardian_demo"] = seed_demo(utcnow())
    is_demo = mode == "Demo data"
    prefix = None
    if is_demo:
        st.warning("DEMO DATA — scripted application-code events and outcomes. No Cortex calls, sandbox operations, GitHub merges, PR comments, or Slack messages are performed.")
        data = st.session_state["guardian_demo"]
        tick_demo(data, utcnow())
        st.sidebar.caption("Demo data lives only in this viewer session. Use Manual backup to replay a scenario.")
    else:
        st.info("TEAM DATA — reads the selected Snowflake tables. Connection errors never switch you to fake data.")
        raw_prefix = st.sidebar.text_input("Team database.schema", os.getenv("GUARDIAN_TABLE_PREFIX", "PR_GUARDIAN.APP"))
        try:
            prefix = qualified_prefix(raw_prefix)
            team_repo = st.sidebar.text_input("GitHub repository", os.getenv("GUARDIAN_REPO", "chezzz439-arch/frosty"))
            validate_pr(team_repo, 1)
            data = read_team(st, prefix, team_repo)
        except Exception as exc:
            st.error("Could not load team data. Check the table contract and the app role's SELECT permissions.")
            st.code(str(exc), language=None)
            st.stop()
        st.sidebar.caption("Latest 1,000 rows per table. Guide tables group by PR number; they do not distinguish reruns or commits.")
        if any(str(r.get("reason", "")).startswith("[DEMO]") for r in data["receipts"]):
            st.warning("These Snowflake tables contain labeled synthetic demo rows. Their outcomes are simulated.")

    runs = sorted(data["runs"], key=lambda r: str(r["created_at"]), reverse=True)
    a, b, c, d = st.columns(4)
    a.metric("Runs in view", len(runs))
    b.metric("Receipts", len(data["receipts"]))
    c.metric("Escalations", sum(r["decision"] == "escalate" for r in data["receipts"]))
    d.metric("Active runs", sum(r.get("state") in ["queued", "running"] for r in runs))
    st.caption("Last refreshed " + utcnow().strftime("%H:%M:%S UTC") + (" · Demo session" if is_demo else " · Snowflake"))

    if page == "Live feed":
        left, right = st.columns([2, 1])
        with left:
            st.subheader("Live PR event feed")
            if not runs:
                st.info("No runs yet. Waiting for the team's first PR event.")
            else:
                for run in runs:
                    selected = run["run_id"]
                    # VERDICTS.DECISION is fix / allow / escalate; no row yet means no verdict.
                    decision = str(run.get("decision") or "no verdict yet").lower()
                    color = {"fix": "#2463b5", "allow": "#17745b", "escalate": "#99620c"}.get(decision, "#526b7b")
                    with st.expander(f"{run['repo']} #{run['pr_number']} · {run['state']}", expanded=True):
                        st.markdown(f'<b style="color:{color}">{html.escape(decision.upper())}</b>', unsafe_allow_html=True)
                        confidence = run.get("confidence")
                        st.caption("Confidence: " + (f"{float(confidence):.0%}" if confidence is not None else "Not supplied"))
                        if run.get("reason"):
                            st.write(str(run["reason"]))
                        st.caption("Run " + selected + " · Commit " + str(run["head_sha"]))
                        events = sorted([e for e in data["events"] if e["run_id"] == selected], key=lambda e: str(e["ts"]))
                        if is_demo:
                            st.progress(min(1.0, run["next_event"] / len(SCENARIOS[run["scenario"]])))
                        if not events:
                            st.info("Waiting for the first pipeline step.")
                        for e in events:
                            status = str(e["status"]).lower()
                            # ok / failed / running are the only statuses written.
                            cls = {"failed": "failed", "running": "queued"}.get(status, "passed")
                            title = html.escape(str(e["step"]).replace("_", " ").title())
                            detail = html.escape(str(e.get("detail") or ""))
                            stamp = html.escape(str(e["ts"]))
                            st.markdown(f'<div class="event {cls}"><small>{stamp} · {html.escape(status.upper())}</small><br><b>{title}</b><p>{detail}</p></div>', unsafe_allow_html=True)
                        receipt = next((r for r in data["receipts"] if r["run_id"] == selected), None)
                        if receipt:
                            st.success("Decision receipt available: " + receipt["decision"].upper())
        with right:
            st.subheader("Run monitor")
            st.caption("Newest PR activity first. Demo runs have separate IDs; the guide’s team tables group events by PR number.")
            for run in runs[:8]:
                st.markdown(f"**{run['repo']} #{run['pr_number']}**")
                st.caption(f"{run['state']} · {run['run_id'][:8]}")
            if is_demo:
                st.info("Try Fix + retry, Allow decoy, Escalate, or Failed run in Manual backup.")

    elif page == "Receipt history":
        st.subheader("Decision receipts")
        term = st.text_input("Search PR, repository, run, or reason")
        decisions = st.multiselect("Decision", ["fix", "allow", "escalate"], default=["fix", "allow", "escalate"])
        receipts = filter_receipts(data["receipts"], term, decisions) if decisions else []
        receipts = sorted(receipts, key=lambda r: str(r["created_at"]), reverse=True)
        if not receipts:
            st.info("No receipts match these filters.")
        for r in receipts:
            with st.expander(f"{r['decision'].upper()} · {r['repo']} #{r['pr_number']} · {str(r['created_at'])}"):
                st.write(r["reason"])
                st.caption("Run " + r["run_id"] + " · Commit " + str(r["head_sha"]))
                row_is_demo = is_demo or str(r.get("reason", "")).startswith("[DEMO]")
                st.write(("Simulated merge: " if row_is_demo else "Merged: ") + ("Yes" if r["merged"] else "No"))
                url = safe_receipt_url(r.get("receipt_url"))
                if url:
                    st.markdown("[View GitHub receipt](" + url + ")")
                elif row_is_demo:
                    st.caption("Synthetic receipt — no real PR or comment exists.")
                if not row_is_demo:
                    st.markdown(f"[View PR](https://github.com/{r['repo']}/pull/{int(r['pr_number'])})")
        export = [{k: str(v) if isinstance(v, datetime) else v for k, v in r.items()} for r in receipts]
        st.download_button("Download filtered receipts (JSON)", json.dumps(export, indent=2, default=str),
                           file_name="guardian-demo-receipts.json" if is_demo else "guardian-receipts.json", mime="application/json")

    else:
        st.subheader("Manual backup trigger")
        st.write("Recover a missed automatic trigger by queuing a review request.")
        st.caption("Writes a manual-run event for the worker. This button never merges a PR.")
        with st.form("guardian_manual"):
            repo = st.text_input("Repository (owner/repository)", "demo/pr-guardian" if is_demo else team_repo)
            pr = st.number_input("PR number", min_value=1, max_value=2147483647, value=104, step=1)
            scenario = st.selectbox("Synthetic scenario", list(SCENARIOS)) if is_demo else None
            requested = st.form_submit_button("Queue demo run" if is_demo else "Queue review request")
        if requested:
            try:
                validate_pr(repo.strip(), pr)
                if is_demo:
                    request = queue_demo(data, repo.strip(), int(pr), scenario, utcnow())
                    st.session_state["guardian_focus"] = request["run_id"]
                    st.session_state["guardian_last_request"] = request["request_id"]
                    st.success("Demo request queued. Open Live feed to watch the simulated run.")
                else:
                    request_id = str(uuid.uuid4())
                    detail = f"Manual backup requested from dashboard; request_id={request_id}; repo={repo.strip()}"
                    get_session(st).sql(manual_event_sql(prefix), params=[int(pr), detail]).collect()
                    st.session_state["guardian_last_request"] = request_id
                    st.success("Manual-run event written to PR_EVENTS. The worker must pick it up and run the normal checks.")
            except Exception as exc:
                st.error(str(exc))
        if st.session_state.get("guardian_last_request"):
            st.caption("Last request ID: " + st.session_state["guardian_last_request"])
        if is_demo and data["requests"]:
            st.dataframe(pd.DataFrame(data["requests"]), use_container_width=True)

    st.sidebar.caption("Rohan's dashboard · PR Guardian · All timestamps UTC")
    # Refresh only the feed, never while a form or search is open.
    if auto and page == "Live feed":
        time.sleep(3)
        if hasattr(st, "rerun"):
            st.rerun()
        else:
            st.experimental_rerun()


if __name__ == "__main__":
    main()
