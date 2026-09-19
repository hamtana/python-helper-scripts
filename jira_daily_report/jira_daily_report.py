"""
Daily Jira Time Tally — emails a formatted summary each day.
Shows time logged from Monday of the current week up to and including today.

Setup:
  1. Fill in the CONFIG section below with your details
  2. Install dependency:  pip install requests
  3. Run manually:        python jira_daily_report.py
  4. Schedule daily:      see instructions at the bottom of this file
"""

import json
import os
import smtplib
import sys
from datetime import datetime, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import requests
from requests.auth import HTTPBasicAuth

# Load variables from a local .env file if python-dotenv is installed and
# a .env file is present. Safe to leave in even when using real environment
# variables (cron, GitHub Actions secrets, etc.) — it just does nothing then.
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# ─────────────────────────────────────────────
#  CONFIG — secrets come from environment variables (see .env.example
#  and "HOW TO SET UP YOUR .env FILE" at the bottom of this file)
# ─────────────────────────────────────────────

def require_env(name):
    """Fetch a required env var, or exit with a clear error instead of
    failing later with a confusing API error."""
    value = os.environ.get(name)
    if not value:
        sys.exit(
            f"Missing required environment variable: {name}\n"
            f"Set it in your .env file or your shell/CI secrets. "
            f"See .env.example for the full list."
        )
    return value

# ─────────────────────────────────────────────
#  CONFIG — fill these in
# ─────────────────────────────────────────────

JIRA_BASE_URL   = os.environ.get("JIRA_BASE_URL")  # your Jira base URL
JIRA_EMAIL      = os.environ.get("JIRA_EMAIL") # your Atlassian account email
JIRA_API_TOKEN  =  os.environ.get("JIRA_API_TOKEN") # see note below on how to get this
JIRA_PROJECT    = os.environ.get("JIRA_PROJECT") # your project key

EMAIL_FROM      = os.environ.get("EMAIL_FROM") # sender (same as your Gmail)
EMAIL_TO        = os.environ.get("EMAIL_TO") # where to send the report
EMAIL_PASSWORD  = os.environ.get("EMAIL_PASSWORD") # see note below on app passwords
SMTP_SERVER     = os.environ.get("SMTP_SERVER")
SMTP_PORT       = os.environ.get("SMTP_PORT")

# ─────────────────────────────────────────────
#  DATE HELPERS
# ─────────────────────────────────────────────

def get_week_range():
    """Return (monday, today) as YYYY-MM-DD strings for the current week."""
    today = datetime.now()
    monday = today - timedelta(days=today.weekday())  # weekday() is 0 for Monday
    return monday.strftime("%Y-%m-%d"), today.strftime("%Y-%m-%d")

# ─────────────────────────────────────────────
#  FETCH ISSUES FROM JIRA
# ─────────────────────────────────────────────

def fetch_worklogs_for_issue(issue_key, since_date, until_date, auth, headers):
    """Fetch worklogs for a single issue and return only those in the date range logged by currentUser."""
    url = f"{JIRA_BASE_URL}/rest/api/3/issue/{issue_key}/worklog"
    response = requests.get(url, headers=headers, auth=auth)
    if response.status_code != 200:
        return 0

    data = response.json()
    total_seconds = 0

    for log in data.get("worklogs", []):
        author_email = log.get("author", {}).get("emailAddress", "")
        if author_email.lower() != JIRA_EMAIL.lower():
            continue

        started = log.get("started", "")[:10]  # YYYY-MM-DD
        if since_date <= started <= until_date:
            total_seconds += log.get("timeSpentSeconds", 0)

    return total_seconds


def fetch_issues():
    """Pull issues where the current user logged work from Monday to today."""
    since, until = get_week_range()

    jql = (
        f'project = {JIRA_PROJECT} '
        f'AND worklogAuthor = currentUser() '
        f'AND worklogDate >= "{since}" '
        f'AND worklogDate <= "{until}"'
    )

    url = f"{JIRA_BASE_URL}/rest/api/3/search/jql"
    auth = HTTPBasicAuth(JIRA_EMAIL, JIRA_API_TOKEN)
    headers = {"Accept": "application/json"}

    raw_issues = []
    start = 0
    max_results = 50

    while True:
        params = {
            "jql": jql,
            "startAt": start,
            "maxResults": max_results,
            "fields": "summary,status,assignee"
        }

        response = requests.get(url, headers=headers, auth=auth, params=params)
        if response.status_code != 200:
            raise Exception(f"Jira API error {response.status_code}: {response.text}")

        data = response.json()
        raw_issues.extend(data["issues"])

        if start + max_results >= data.get("totalCount", data.get("total", 0)):
            break
        start += max_results

    # Fetch actual time logged in range for each issue
    issues = []
    for issue in raw_issues:
        key = issue["key"]
        seconds_in_range = fetch_worklogs_for_issue(key, since, until, auth, headers)
        if seconds_in_range > 0:
            issue["fields"]["timespent"] = seconds_in_range
            issues.append(issue)

    return issues, since, until

# ─────────────────────────────────────────────
#  FORMAT THE REPORT
# ─────────────────────────────────────────────

def seconds_to_hours(seconds):
    if not seconds:
        return "0.0h"
    return f"{seconds / 3600:.2f}h"

def build_report(issues, since, until):
    """Build a plain-text and HTML version of the week-to-date tally."""
    week_start  = datetime.strptime(since, "%Y-%m-%d").strftime("%d %b %Y")
    today_label = datetime.strptime(until, "%Y-%m-%d").strftime("%d %b %Y")
    today_day   = datetime.strptime(until, "%Y-%m-%d").strftime("%A")  # e.g. "Wednesday"

    total_seconds = sum(
        (i["fields"].get("timespent") or 0) for i in issues
    )

    # ── Plain text ──
    separator = "-" * 62
    plain_lines = [
        f"Daily Time Report — {JIRA_PROJECT}",
        f"Week to date: {week_start} to {today_label} ({today_day})",
        separator,
        f"{'Issue':<12} {'Time':>6}  {'Status':<18} Summary",
        separator,
    ]

    for issue in sorted(issues, key=lambda x: int(x["key"].split("-")[1])):
        fields  = issue["fields"]
        key     = issue["key"]
        summary = fields.get("summary", "")[:45]
        spent   = seconds_to_hours(fields.get("timespent"))
        status  = fields.get("status", {}).get("name", "")[:16]
        plain_lines.append(f"{key:<12} {spent:>6}  {status:<18} {summary}")

    plain_lines += [
        separator,
        f"{'TOTAL':<12} {seconds_to_hours(total_seconds):>6}",
        "",
        f"Issues logged: {len(issues)}",
        f"Full report: {JIRA_BASE_URL}/jira/servicedesk/projects/{JIRA_PROJECT}/reports",
    ]

    plain_text = "\n".join(plain_lines)

    # ── HTML ──
    rows_html = ""
    for issue in sorted(issues, key=lambda x: int(x["key"].split("-")[1])):
        fields    = issue["fields"]
        key       = issue["key"]
        summary   = fields.get("summary", "")
        spent     = seconds_to_hours(fields.get("timespent"))
        status    = fields.get("status", {}).get("name", "")
        issue_url = f"{JIRA_BASE_URL}/browse/{key}"
        rows_html += f"""
        <tr>
          <td><a href="{issue_url}" style="color:#185FA5;text-decoration:none;">{key}</a></td>
          <td>{summary}</td>
          <td style="text-align:center;">{status}</td>
          <td style="text-align:right;font-weight:500;">{spent}</td>
        </tr>"""

    html = f"""
    <html><body style="font-family:Arial,sans-serif;color:#333;max-width:700px;margin:0 auto;padding:20px;">
      <h2 style="color:#185FA5;border-bottom:2px solid #185FA5;padding-bottom:8px;">
        Daily Time Report — {JIRA_PROJECT}
      </h2>
      <p style="color:#666;font-size:14px;">
        Week to date &mdash; {week_start} &rarr; {today_label} ({today_day})
      </p>

      <table width="100%" cellpadding="8" cellspacing="0"
             style="border-collapse:collapse;font-size:14px;">
        <thead>
          <tr style="background:#E6F1FB;color:#0C447C;">
            <th style="text-align:left;border-bottom:1px solid #B5D4F4;">Issue</th>
            <th style="text-align:left;border-bottom:1px solid #B5D4F4;">Summary</th>
            <th style="text-align:center;border-bottom:1px solid #B5D4F4;">Status</th>
            <th style="text-align:right;border-bottom:1px solid #B5D4F4;">Time</th>
          </tr>
        </thead>
        <tbody>{rows_html}
        </tbody>
        <tfoot>
          <tr style="background:#f5f5f5;font-weight:bold;">
            <td colspan="3" style="border-top:2px solid #B5D4F4;padding-top:10px;">
              Total — {len(issues)} issue(s)
            </td>
            <td style="text-align:right;border-top:2px solid #B5D4F4;
                       padding-top:10px;color:#185FA5;font-size:16px;">
              {seconds_to_hours(total_seconds)}
            </td>
          </tr>
        </tfoot>
      </table>

      <p style="margin-top:20px;">
        <a href="{JIRA_BASE_URL}/jira/servicedesk/projects/{JIRA_PROJECT}/reports"
           style="background:#185FA5;color:white;padding:8px 16px;
                  border-radius:6px;text-decoration:none;font-size:14px;">
          View full report in Jira &rarr;
        </a>
      </p>

      <p style="color:#aaa;font-size:12px;margin-top:30px;">
        Sent automatically by your Jira daily report script.
      </p>
    </body></html>
    """

    return plain_text, html

# ─────────────────────────────────────────────
#  SEND THE EMAIL
# ─────────────────────────────────────────────

def send_email(plain_text, html, until):
    today_label = datetime.strptime(until, "%Y-%m-%d").strftime("%d %b %Y")
    today_day   = datetime.strptime(until, "%Y-%m-%d").strftime("%a")  # e.g. "Wed"
    subject     = f"Time report — {JIRA_PROJECT} — WTD to {today_day} {today_label}"

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = EMAIL_FROM
    msg["To"]      = EMAIL_TO

    msg.attach(MIMEText(plain_text, "plain"))
    msg.attach(MIMEText(html, "html"))

    with smtplib.SMTP(SMTP_SERVER, SMTP_PORT) as server:
        server.starttls()
        server.login(EMAIL_FROM, EMAIL_PASSWORD)
        server.sendmail(EMAIL_FROM, EMAIL_TO, msg.as_string())

    print(f"Email sent to {EMAIL_TO}")

# ─────────────────────────────────────────────
#  MAIN
# ─────────────────────────────────────────────

if __name__ == "__main__":
    print("Fetching Jira issues...")
    issues, since, until = fetch_issues()
    print(f"Found {len(issues)} issue(s) with time logged.")

    if not issues:
        print("No issues found for this week so far. No email sent.")
    else:
        plain_text, html = build_report(issues, since, until)
        print("\n--- Report preview ---")
        print(plain_text)
        print("----------------------\n")
        send_email(plain_text, html, until)
        print("Done.")


# ─────────────────────────────────────────────
#  HOW TO SCHEDULE THIS DAILY
# ─────────────────────────────────────────────
#
#  Mac / Linux — cron job (runs Mon–Fri at 6pm):
#    Open terminal and run:  crontab -e
#    Add this line:
#      0 18 * * 1-5 /usr/bin/python3 /path/to/jira_daily_report.py
#
#    To run every day including weekends:
#      0 18 * * * /usr/bin/python3 /path/to/jira_daily_report.py
#
#  Windows — Task Scheduler:
#    1. Open Task Scheduler → Create Basic Task
#    2. Trigger: Daily, 6:00 PM
#    3. Action: Start a program → python.exe
#    4. Arguments: C:\path\to\jira_daily_report.py
#
#  GitHub Actions (runs Mon–Fri at 6am UTC = 6pm NZT):
#    Add a workflow file .github/workflows/daily_report.yml:
#
#      name: Daily Jira Report
#      on:
#        schedule:
#          - cron: '0 6 * * 1-5'
#      jobs:
#        report:
#          runs-on: ubuntu-latest
#          steps:
#            - uses: actions/checkout@v4
#            - uses: actions/setup-python@v5
#              with:
#                python-version: '3.11'
#            - run: pip install requests
#            - run: python jira_daily_report.py
#              env:
#                # Store secrets in GitHub repo Settings → Secrets
#                JIRA_API_TOKEN: ${{ secrets.JIRA_API_TOKEN }}
#                EMAIL_PASSWORD: ${{ secrets.EMAIL_PASSWORD }}
#
#    Then update the CONFIG section to read from env vars:
#      import os
#      JIRA_API_TOKEN = os.environ["JIRA_API_TOKEN"]
#      EMAIL_PASSWORD = os.environ["EMAIL_PASSWORD"]
