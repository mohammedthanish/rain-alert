#!/usr/bin/env python3
"""Email an alert when the rain-alert automation itself is broken.

The rain watcher only emails when there is something to say, so a quiet
inbox is normal - this script is what turns "the watcher died" into an
email instead of silence. It runs from healthcheck.yml in two modes:

  --mode failure   Triggered by workflow_run whenever a "Rain alerts"
                   run completes with conclusion=failure. Emails the
                   run URL. To avoid 96 emails/day while broken, it
                   only mails when the PREVIOUS completed run had
                   succeeded (i.e. the first failure of a streak).

  --mode stale     Runs on its own schedule. The watcher should
                   complete a successful run every ~15 minutes; if the
                   most recent success is older than STALE_HOURS
                   (default 2), something is wrong that never even
                   produced a failing run (schedule disabled, workflow
                   file broken, runner backlog) - email about it.

Known limitation: if GitHub Actions' scheduler is down entirely, this
check does not run either. The watcher mitigates the one predictable
cause (schedules auto-disable after 60 days without repo activity) by
committing state.json daily.

Stdlib only. Needs env: GITHUB_TOKEN, GITHUB_REPOSITORY, SMTP_USER,
SMTP_PASS, EMAIL_TO (and optionally SMTP_HOST/SMTP_PORT/EMAIL_FROM,
STALE_HOURS, FAILED_RUN_ID/FAILED_RUN_URL/FAILED_RUN_EVENT in failure
mode).
"""

import json
import os
import smtplib
import ssl
import sys
import urllib.request
from datetime import datetime, timedelta, timezone

WATCH_WORKFLOW = "check.yml"


def gh_api(path: str):
    repo = os.environ["GITHUB_REPOSITORY"]
    req = urllib.request.Request(
        f"https://api.github.com/repos/{repo}{path}",
        headers={
            "Authorization": f"Bearer {os.environ['GITHUB_TOKEN']}",
            "Accept": "application/vnd.github+json",
            "User-Agent": "rain-alert-healthcheck",
        })
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read().decode("utf-8"))


def parse_ts(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def send_email(subject: str, lines: list) -> None:
    host = os.environ.get("SMTP_HOST", "smtp.gmail.com")
    port = int(os.environ.get("SMTP_PORT", "465"))
    user = os.environ["SMTP_USER"]
    sender = os.environ.get("EMAIL_FROM", user)

    from email.message import EmailMessage
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = os.environ["EMAIL_TO"]
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    body = list(lines) + [
        "",
        f"Actions: https://github.com/{repo}/actions",
        "",
        "(Automated healthcheck of the rain-alert watcher. While this is",
        "broken you will NOT get rain alerts - do not trust the silence.)",
    ]
    msg.set_content("\n".join(body))

    context = ssl.create_default_context()
    if port == 465:
        with smtplib.SMTP_SSL(host, port, context=context, timeout=60) as server:
            server.login(user, os.environ["SMTP_PASS"])
            server.send_message(msg)
    else:
        with smtplib.SMTP(host, port, timeout=60) as server:
            server.starttls(context=context)
            server.login(user, os.environ["SMTP_PASS"])
            server.send_message(msg)
    print(f"Healthcheck email sent: {subject}")


def runs(params: str) -> list:
    data = gh_api(f"/actions/workflows/{WATCH_WORKFLOW}/runs?{params}")
    return data.get("workflow_runs", [])


def mode_failure() -> int:
    run_id = os.environ.get("FAILED_RUN_ID", "?")
    run_url = os.environ.get("FAILED_RUN_URL", "")
    event = os.environ.get("FAILED_RUN_EVENT", "?")

    # First failure of a streak? Look at the completed run just before
    # this one; if that also failed, an email already went out.
    completed = runs("status=completed&per_page=10")
    idx = next((i for i, r in enumerate(completed)
                if str(r["id"]) == str(run_id)), None)
    if idx is not None:
        for prev in completed[idx + 1:]:
            if prev.get("conclusion") == "cancelled":
                continue  # cancellations say nothing about health
            if prev.get("conclusion") == "failure":
                print("Previous run also failed; alert already sent, skipping.")
                return 0
            break

    send_email(
        "Rain watcher BROKEN: a run just failed",
        [f"A '{event}' run of the rain watcher failed.",
         f"Run: {run_url or run_id}",
         "",
         "Later runs will retry automatically, but if this keeps failing",
         "the staleness check will follow up every few hours."])
    return 0


def mode_stale() -> int:
    stale_hours = float(os.environ.get("STALE_HOURS", "2"))
    recent = runs("per_page=10")
    now = datetime.now(timezone.utc)

    success = next((r for r in recent if r.get("conclusion") == "success"), None)
    if success:
        age = now - parse_ts(success["created_at"])
        if age <= timedelta(hours=stale_hours):
            print(f"Healthy: last success {int(age.total_seconds() // 60)} min ago.")
            return 0

    if not recent:
        detail = "No runs of the watcher exist at all."
    elif success:
        age_h = (now - parse_ts(success["created_at"])).total_seconds() / 3600
        last = recent[0]
        detail = (f"Last SUCCESSFUL run was {age_h:.1f}h ago "
                  f"(expected every ~15 min). Most recent run: "
                  f"{last.get('status')}/{last.get('conclusion')} - "
                  f"{last.get('html_url')}")
    else:
        last = recent[0]
        detail = (f"No successful run in the last {len(recent)} attempts. "
                  f"Most recent: {last.get('status')}/{last.get('conclusion')} - "
                  f"{last.get('html_url')}")

    send_email(
        "Rain watcher BROKEN: no successful runs",
        ["The rain watcher has not completed a successful check recently.",
         detail])
    return 0


def main(argv: list) -> int:
    mode = argv[argv.index("--mode") + 1] if "--mode" in argv else "stale"
    try:
        if mode == "failure":
            return mode_failure()
        return mode_stale()
    except Exception as exc:
        # A broken healthcheck must fail loudly in the Actions UI.
        print(f"ERROR: healthcheck itself failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
