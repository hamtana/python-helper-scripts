"""
Weekly Jira Time Tally — emails a formatted summary every week and
creates a draft invoice in Zoho Invoice.

Setup:
  1. Copy .env.example to .env and fill in your own values
  2. Install dependencies:  pip install requests python-dotenv
  3. Run manually:          python jira_weekly_report.py
  4. Schedule weekly:       see instructions at the bottom of this file

All secrets (Jira token, email password, Zoho credentials) are read from
environment variables — nothing sensitive lives in this file. See
.env.example and "HOW TO SET UP YOUR .env FILE" at the bottom.
"""

import os
import sys
import requests
import smtplib
import json
from datetime import datetime, timedelta
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
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

# Non-secret config — fine to leave as plain values, or override via env too
JIRA_BASE_URL = os.environ.get("JIRA_BASE_URL")
JIRA_PROJECT  = os.environ.get("JIRA_PROJECT")
SMTP_SERVER   = os.environ.get("SMTP_SERVER")
SMTP_PORT     = int(os.environ.get("SMTP_PORT"))
ZOHO_ACCOUNTS_DOMAIN = os.environ.get("ZOHO_ACCOUNTS_DOMAIN")
ZOHO_API_DOMAIN      = os.environ.get("ZOHO_API_DOMAIN")
HOURLY_RATE              = float(os.environ.get("HOURLY_RATE", "0.00"))
INVOICE_CURRENCY_SYMBOL  = os.environ.get("INVOICE_CURRENCY_SYMBOL", "$")

# Secrets — must be set in the environment, no fallback/defaults
JIRA_EMAIL      = os.environ.get("JIRA_EMAIL")
JIRA_API_TOKEN  = os.environ.get("JIRA_API_TOKEN")

EMAIL_FROM      = os.environ.get("EMAIL_FROM")
EMAIL_TO        = os.environ.get("EMAIL_TO")
EMAIL_PASSWORD  = os.environ.get("EMAIL_PASSWORD")

ZOHO_CLIENT_ID        = os.environ.get("ZOHO_CLIENT_ID")
ZOHO_CLIENT_SECRET    = os.environ.get("ZOHO_CLIENT_SECRET")
ZOHO_REFRESH_TOKEN    = os.environ.get("ZOHO_REFRESH_TOKEN")
ZOHO_ORGANIZATION_ID  = os.environ.get("ZOHO_ORGANIZATION_ID")
ZOHO_CUSTOMER_ID      = os.environ.get("ZOHO_CUSTOMER_ID")

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
        # worklog author check
        author_email = log.get("author", {}).get("emailAddress", "")
        if author_email.lower() != JIRA_EMAIL.lower():
            continue

        # worklog date check — "started" is like "2026-06-03T09:00:00.000+0000"
        started = log.get("started", "")[:10]  # grab YYYY-MM-DD
        if since_date <= started <= until_date:
            total_seconds += log.get("timeSpentSeconds", 0)

    return total_seconds


def fetch_issues():
    """Pull issues where the current user logged work in the date range, with only that range's time."""
    since = (datetime.now() - timedelta(days=6)).strftime("%Y-%m-%d")
    until = datetime.now().strftime("%Y-%m-%d")

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
            "fields": "summary,status,assignee"  # removed timespent — we calculate it ourselves
        }

        response = requests.get(url, headers=headers, auth=auth, params=params)
        if response.status_code != 200:
            raise Exception(f"Jira API error {response.status_code}: {response.text}")

        data = response.json()
        raw_issues.extend(data["issues"])

        if start + max_results >= data.get("totalCount", data.get("total", 0)):
            break
        start += max_results

    # Now fetch actual time logged in range for each issue
    issues = []
    for issue in raw_issues:
        key = issue["key"]
        seconds_in_range = fetch_worklogs_for_issue(key, since, until, auth, headers)
        if seconds_in_range > 0:
            issue["fields"]["timespent"] = seconds_in_range  # overwrite with range-only value
            issues.append(issue)

    return issues

# ─────────────────────────────────────────────
#  FORMAT THE REPORT
# ─────────────────────────────────────────────

def seconds_to_hours(seconds):
    if not seconds:
        return "0.0h"
    hours = seconds / 3600
    return f"{hours:.2f}h"

def build_report(issues):
    """Build a plain-text and HTML version of the weekly tally."""
    week_end   = datetime.now().strftime("%d %b %Y")
    week_start = (datetime.now() - timedelta(days=6)).strftime("%d %b %Y")

    total_seconds = sum(
        (i["fields"].get("timespent") or 0) for i in issues
    )

    # ── Plain text version ──
    separator = "-" * 62
    plain_lines = [
        f"Weekly Time Report — {JIRA_PROJECT}",
        f"{week_start} to {week_end}",
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

    # ── HTML version ──
    rows_html = ""
    for issue in sorted(issues, key=lambda x: int(x["key"].split("-")[1])):
        fields  = issue["fields"]
        key     = issue["key"]
        summary = fields.get("summary", "")
        spent   = seconds_to_hours(fields.get("timespent"))
        status  = fields.get("status", {}).get("name", "")
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
        Weekly Time Report — {JIRA_PROJECT}
      </h2>
      <p style="color:#666;font-size:14px;">{week_start} &rarr; {week_end}</p>

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
        Sent automatically by your Jira weekly report script.
      </p>
    </body></html>
    """

    return plain_text, html

# ─────────────────────────────────────────────
#  SEND THE EMAIL
# ─────────────────────────────────────────────

def send_email(plain_text, html):
    week_end = datetime.now().strftime("%d %b %Y")
    subject  = f"Weekly time report — {JIRA_PROJECT} — w/e {week_end}"

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
#  BUILD & CREATE THE ZOHO INVOICE (DRAFT)
# ─────────────────────────────────────────────

def get_zoho_access_token():
    """Exchange the long-lived refresh token for a short-lived access token."""
    url = f"{ZOHO_ACCOUNTS_DOMAIN}/oauth/v2/token"
    params = {
        "refresh_token": ZOHO_REFRESH_TOKEN,
        "client_id": ZOHO_CLIENT_ID,
        "client_secret": ZOHO_CLIENT_SECRET,
        "grant_type": "refresh_token",
    }
    response = requests.post(url, params=params)
    if response.status_code != 200:
        raise Exception(f"Zoho auth error {response.status_code}: {response.text}")

    data = response.json()
    if "access_token" not in data:
        raise Exception(f"Zoho auth response missing access_token: {data}")

    return data["access_token"]


def build_zoho_line_items(issues):
    """One invoice line per Jira issue, quantity = hours logged that week."""
    line_items = []
    for issue in sorted(issues, key=lambda x: int(x["key"].split("-")[1])):
        key     = issue["key"]
        summary = issue["fields"].get("summary", "")
        hours   = round((issue["fields"].get("timespent") or 0) / 3600, 2)

        line_items.append({
            "name": key,
            "description": summary,
            "rate": HOURLY_RATE,
            "quantity": hours,
        })
    return line_items


def create_zoho_invoice(issues):
    """Create a DRAFT invoice in Zoho Invoice (not sent to the client)."""
    access_token = get_zoho_access_token()

    week_end   = datetime.now().strftime("%Y-%m-%d")
    week_start = (datetime.now() - timedelta(days=6)).strftime("%Y-%m-%d")

    payload = {
        "customer_id": ZOHO_CUSTOMER_ID,
        "date": week_end,
        "reference_number": f"{JIRA_PROJECT} {week_start} to {week_end}",
        "line_items": build_zoho_line_items(issues),
    }

    url = f"{ZOHO_API_DOMAIN}/invoice/v3/invoices"
    headers = {
        "Authorization": f"Zoho-oauthtoken {access_token}",
        "X-com-zoho-invoice-organizationid": ZOHO_ORGANIZATION_ID,
        "Content-Type": "application/json",
    }
    params = {"organization_id": ZOHO_ORGANIZATION_ID}

    response = requests.post(url, headers=headers, params=params, json=payload)
    if response.status_code not in (200, 201):
        raise Exception(f"Zoho Invoice error {response.status_code}: {response.text}")

    data = response.json()
    invoice = data.get("invoice", {})
    print(
        f"Draft invoice created in Zoho: "
        f"{invoice.get('invoice_number', '(number pending)')} "
        f"— total {INVOICE_CURRENCY_SYMBOL}{invoice.get('total', 0)}"
    )
    return invoice

# ─────────────────────────────────────────────
#  MAIN
# ─────────────────────────────────────────────

if __name__ == "__main__":
    print("Fetching Jira issues...")
    issues = fetch_issues()
    print(f"Found {len(issues)} issue(s) with time logged.")

    if not issues:
        print("No issues found for the past 7 days. No email or invoice created.")
    else:
        plain_text, html = build_report(issues)
        print("\n--- Report preview ---")
        print(plain_text)
        print("----------------------\n")

        send_email(plain_text, html)

        print("Creating draft invoice in Zoho Invoice...")
        create_zoho_invoice(issues)

        print("Done.")


# ─────────────────────────────────────────────
#  HOW TO SET UP YOUR .env FILE
# ─────────────────────────────────────────────
#
#  1. In the same folder as this script, copy .env.example to a new
#     file named exactly ".env"
#  2. Fill in every value in .env (see the sections below for where to
#     get each one — Jira token, Gmail app password, Zoho credentials)
#  3. Install python-dotenv so the script can read it:
#       pip install python-dotenv
#  4. NEVER commit .env to git or share it. Add this line to your
#     .gitignore if you keep this script in a repo:
#       .env
#
#  If you're running this on a schedule via cron or Task Scheduler
#  (not GitHub Actions), the .env file approach above works as-is —
#  just make sure .env sits next to the script on that machine.
#
#  If you're running this via GitHub Actions, don't use a .env file at
#  all. Instead add each value as a Repository Secret (Settings →
#  Secrets and variables → Actions → New repository secret), then
#  reference them in your workflow file, e.g.:
#
#    - name: Run weekly report
#      run: python jira_weekly_report.py
#      env:
#        JIRA_EMAIL: ${{ secrets.JIRA_EMAIL }}
#        JIRA_API_TOKEN: ${{ secrets.JIRA_API_TOKEN }}
#        EMAIL_FROM: ${{ secrets.EMAIL_FROM }}
#        EMAIL_TO: ${{ secrets.EMAIL_TO }}
#        EMAIL_PASSWORD: ${{ secrets.EMAIL_PASSWORD }}
#        ZOHO_CLIENT_ID: ${{ secrets.ZOHO_CLIENT_ID }}
#        ZOHO_CLIENT_SECRET: ${{ secrets.ZOHO_CLIENT_SECRET }}
#        ZOHO_REFRESH_TOKEN: ${{ secrets.ZOHO_REFRESH_TOKEN }}
#        ZOHO_ORGANIZATION_ID: ${{ secrets.ZOHO_ORGANIZATION_ID }}
#        ZOHO_CUSTOMER_ID: ${{ secrets.ZOHO_CUSTOMER_ID }}
#
# ─────────────────────────────────────────────
#  HOW TO GET YOUR JIRA API TOKEN
# ─────────────────────────────────────────────
#
#  1. Go to: https://id.atlassian.com/manage-profile/security/api-tokens
#  2. Click "Create API token"
#  3. Give it a name like "Weekly report script"
#  4. Copy the token — you'll paste it as JIRA_API_TOKEN in your .env file
#
# ─────────────────────────────────────────────
#  HOW TO GET A GMAIL APP PASSWORD
# ─────────────────────────────────────────────
#
#  Gmail blocks plain passwords for scripts. Use an App Password instead:
#  1. Go to: https://myaccount.google.com/apppasswords
#     (requires 2-factor authentication to be enabled on your Google account)
#  2. Select app: "Mail", device: "Other" → type "Jira report"
#  3. Copy the 16-character password — you'll paste it as EMAIL_PASSWORD in your .env file
#
#  Not using Gmail? Change SMTP_SERVER and SMTP_PORT:
#    Outlook / Office 365:  smtp.office365.com, port 587
#    Yahoo:                 smtp.mail.yahoo.com, port 587
#
# ─────────────────────────────────────────────
#  HOW TO SET UP THE ZOHO INVOICE API (one-time setup)
# ─────────────────────────────────────────────
#
#  Zoho uses OAuth2, so there's a bit more setup than Jira, but you only
#  do this once — after that the script refreshes its own access token.
#
#  1. Register a "Self Client" to get a client ID/secret:
#     - Go to https://api-console.zoho.com.au/   (AU data center console)
#     - Click "Add Client" → "Self Client" → Create
#     - Copy the Client ID and Client Secret into ZOHO_CLIENT_ID and
#       ZOHO_CLIENT_SECRET in your .env file
#
#  2. Generate an authorization code (in the same Self Client screen,
#     under the "Generate Code" tab):
#     - Scope:   ZohoInvoice.invoices.CREATE,ZohoInvoice.invoices.READ
#     - Duration: choose the longest option available (e.g. 10 minutes —
#       you just need it long enough to complete step 3 immediately after)
#     - Click Create, copy the generated code
#
#  3. Exchange that code for a refresh token by running this once
#     (replace the placeholders, then run from a terminal):
#
#       curl -X POST https://accounts.zoho.com.au/oauth/v2/token \
#         -d "grant_type=authorization_code" \
#         -d "client_id=YOUR_CLIENT_ID" \
#         -d "client_secret=YOUR_CLIENT_SECRET" \
#         -d "redirect_uri=https://www.zoho.com/invoice" \
#         -d "code=THE_CODE_FROM_STEP_2"
#
#     The JSON response includes a "refresh_token" — copy that into
#     ZOHO_REFRESH_TOKEN in your .env file. It does not expire (unless revoked).
#
#  4. Find your Organization ID:
#     - In Zoho Invoice, go to Settings → Organization Profile
#     - Copy the Organization ID into ZOHO_ORGANIZATION_ID in your .env file
#
#  5. Find (or create) the Customer ID for this client:
#     - In Zoho Invoice, go to Customers, open the client, and copy the
#       ID from the URL, OR fetch it via the API:
#
#       curl https://www.zohoapis.com.au/invoice/v3/contacts?organization_id=YOUR_ORG_ID \
#         -H "Zoho-oauthtoken YOUR_ACCESS_TOKEN"
#
#     Copy the matching contact_id into ZOHO_CUSTOMER_ID in your .env file.
#
#  6. Set HOURLY_RATE to your billing rate.
#
#  This script is set up for the Australian Zoho data center
#  (accounts.zoho.com.au / www.zohoapis.com.au). If you ever move
#  organizations to a different data center, update ZOHO_ACCOUNTS_DOMAIN
#  and ZOHO_API_DOMAIN in your .env file accordingly (e.g. .eu, .in, .com for US).
#
#  Note: the invoice is created as a DRAFT — it is NOT emailed to your
#  client automatically. Review it in Zoho Invoice and send it yourself.
#
# ─────────────────────────────────────────────
#  HOW TO SCHEDULE THIS WEEKLY (pick one)
# ─────────────────────────────────────────────
#
#  Mac / Linux — cron job:
#    Open terminal and run:  crontab -e
#    Add this line (runs every Monday at 8am):
#      0 8 * * 1 /usr/bin/python3 /path/to/jira_weekly_report.py
#
#  Windows — Task Scheduler:
#    1. Open Task Scheduler → Create Basic Task
#    2. Trigger: Weekly, Monday, 8:00 AM
#    3. Action: Start a program → python.exe
#    4. Arguments: C:\path\to\jira_weekly_report.py
#
#  Free cloud option — GitHub Actions:
#    Push this script to a private GitHub repo and add a workflow file
#    with "schedule: cron: '0 20 * * 0'" (Sunday 8pm UTC = Monday 8am NZT)
#    Ask Claude to generate the GitHub Actions workflow file if needed.
#    IMPORTANT: do NOT commit your .env file — use GitHub Actions
#    "Repository secrets" instead. See "HOW TO SET UP YOUR .env FILE"
#    above for the exact workflow syntax.
hamish@server-prod:~/projects/invoiceAutomated$ bat invoice_automated.py
Command 'bat' not found, but can be installed with:
sudo apt install bacula-console-qt
hamish@server-prod:~/projects/invoiceAutomated$ cat invoice_automated.py
"""
Weekly Jira Time Tally — emails a formatted summary every week and
creates a draft invoice in Zoho Invoice.

Setup:
  1. Copy .env.example to .env and fill in your own values
  2. Install dependencies:  pip install requests python-dotenv
  3. Run manually:          python jira_weekly_report.py
  4. Schedule weekly:       see instructions at the bottom of this file

All secrets (Jira token, email password, Zoho credentials) are read from
environment variables — nothing sensitive lives in this file. See
.env.example and "HOW TO SET UP YOUR .env FILE" at the bottom.
"""

import os
import sys
import requests
import smtplib
import json
from datetime import datetime, timedelta
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
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

# Non-secret config — fine to leave as plain values, or override via env too
JIRA_BASE_URL = os.environ.get("JIRA_BASE_URL", "https://phillipsmusictech.atlassian.net")
JIRA_PROJECT  = os.environ.get("JIRA_PROJECT", "NIC")
SMTP_SERVER   = os.environ.get("SMTP_SERVER", "smtp.gmail.com")
SMTP_PORT     = int(os.environ.get("SMTP_PORT", "587"))
ZOHO_ACCOUNTS_DOMAIN = os.environ.get("ZOHO_ACCOUNTS_DOMAIN", "https://accounts.zoho.com.au")
ZOHO_API_DOMAIN      = os.environ.get("ZOHO_API_DOMAIN", "https://www.zohoapis.com.au")
HOURLY_RATE              = float(os.environ.get("HOURLY_RATE", "0.00"))
INVOICE_CURRENCY_SYMBOL  = os.environ.get("INVOICE_CURRENCY_SYMBOL", "$")

# Secrets — must be set in the environment, no fallback/defaults
JIRA_EMAIL      = os.environ.get("JIRA_EMAIL")
JIRA_API_TOKEN  = os.environ.get("JIRA_API_TOKEN")

EMAIL_FROM      = os.environ.get("EMAIL_FROM")
EMAIL_TO        = os.environ.get("EMAIL_TO")
EMAIL_PASSWORD  = os.environ.get("EMAIL_PASSWORD")

ZOHO_CLIENT_ID        = os.environ.get("ZOHO_CLIENT_ID")
ZOHO_CLIENT_SECRET    = os.environ.get("ZOHO_CLIENT_SECRET")
ZOHO_REFRESH_TOKEN    = os.environ.get("ZOHO_REFRESH_TOKEN")
ZOHO_ORGANIZATION_ID  = os.environ.get("ZOHO_ORGANIZATION_ID")
ZOHO_CUSTOMER_ID      = os.environ.get("ZOHO_CUSTOMER_ID")

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
        # worklog author check
        author_email = log.get("author", {}).get("emailAddress", "")
        if author_email.lower() != JIRA_EMAIL.lower():
            continue

        # worklog date check — "started" is like "2026-06-03T09:00:00.000+0000"
        started = log.get("started", "")[:10]  # grab YYYY-MM-DD
        if since_date <= started <= until_date:
            total_seconds += log.get("timeSpentSeconds", 0)

    return total_seconds


def fetch_issues():
    """Pull issues where the current user logged work in the date range, with only that range's time."""
    since = (datetime.now() - timedelta(days=6)).strftime("%Y-%m-%d")
    until = datetime.now().strftime("%Y-%m-%d")

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
            "fields": "summary,status,assignee"  # removed timespent — we calculate it ourselves
        }

        response = requests.get(url, headers=headers, auth=auth, params=params)
        if response.status_code != 200:
            raise Exception(f"Jira API error {response.status_code}: {response.text}")

        data = response.json()
        raw_issues.extend(data["issues"])

        if start + max_results >= data.get("totalCount", data.get("total", 0)):
            break
        start += max_results

    # Now fetch actual time logged in range for each issue
    issues = []
    for issue in raw_issues:
        key = issue["key"]
        seconds_in_range = fetch_worklogs_for_issue(key, since, until, auth, headers)
        if seconds_in_range > 0:
            issue["fields"]["timespent"] = seconds_in_range  # overwrite with range-only value
            issues.append(issue)

    return issues

# ─────────────────────────────────────────────
#  FORMAT THE REPORT
# ─────────────────────────────────────────────

def seconds_to_hours(seconds):
    if not seconds:
        return "0.0h"
    hours = seconds / 3600
    return f"{hours:.2f}h"

def build_report(issues):
    """Build a plain-text and HTML version of the weekly tally."""
    week_end   = datetime.now().strftime("%d %b %Y")
    week_start = (datetime.now() - timedelta(days=6)).strftime("%d %b %Y")

    total_seconds = sum(
        (i["fields"].get("timespent") or 0) for i in issues
    )

    # ── Plain text version ──
    separator = "-" * 62
    plain_lines = [
        f"Weekly Time Report — {JIRA_PROJECT}",
        f"{week_start} to {week_end}",
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

    # ── HTML version ──
    rows_html = ""
    for issue in sorted(issues, key=lambda x: int(x["key"].split("-")[1])):
        fields  = issue["fields"]
        key     = issue["key"]
        summary = fields.get("summary", "")
        spent   = seconds_to_hours(fields.get("timespent"))
        status  = fields.get("status", {}).get("name", "")
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
        Weekly Time Report — {JIRA_PROJECT}
      </h2>
      <p style="color:#666;font-size:14px;">{week_start} &rarr; {week_end}</p>

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
        Sent automatically by your Jira weekly report script.
      </p>
    </body></html>
    """

    return plain_text, html

# ─────────────────────────────────────────────
#  SEND THE EMAIL
# ─────────────────────────────────────────────

def send_email(plain_text, html):
    week_end = datetime.now().strftime("%d %b %Y")
    subject  = f"Weekly time report — {JIRA_PROJECT} — w/e {week_end}"

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
#  BUILD & CREATE THE ZOHO INVOICE (DRAFT)
# ─────────────────────────────────────────────

def get_zoho_access_token():
    """Exchange the long-lived refresh token for a short-lived access token."""
    url = f"{ZOHO_ACCOUNTS_DOMAIN}/oauth/v2/token"
    params = {
        "refresh_token": ZOHO_REFRESH_TOKEN,
        "client_id": ZOHO_CLIENT_ID,
        "client_secret": ZOHO_CLIENT_SECRET,
        "grant_type": "refresh_token",
    }
    response = requests.post(url, params=params)
    if response.status_code != 200:
        raise Exception(f"Zoho auth error {response.status_code}: {response.text}")

    data = response.json()
    if "access_token" not in data:
        raise Exception(f"Zoho auth response missing access_token: {data}")

    return data["access_token"]


def build_zoho_line_items(issues):
    """One invoice line per Jira issue, quantity = hours logged that week."""
    line_items = []
    for issue in sorted(issues, key=lambda x: int(x["key"].split("-")[1])):
        key     = issue["key"]
        summary = issue["fields"].get("summary", "")
        hours   = round((issue["fields"].get("timespent") or 0) / 3600, 2)

        line_items.append({
            "name": key,
            "description": summary,
            "rate": HOURLY_RATE,
            "quantity": hours,
        })
    return line_items


def create_zoho_invoice(issues):
    """Create a DRAFT invoice in Zoho Invoice (not sent to the client)."""
    access_token = get_zoho_access_token()

    week_end   = datetime.now().strftime("%Y-%m-%d")
    week_start = (datetime.now() - timedelta(days=6)).strftime("%Y-%m-%d")

    payload = {
        "customer_id": ZOHO_CUSTOMER_ID,
        "date": week_end,
        "reference_number": f"{JIRA_PROJECT} {week_start} to {week_end}",
        "line_items": build_zoho_line_items(issues),
    }

    url = f"{ZOHO_API_DOMAIN}/invoice/v3/invoices"
    headers = {
        "Authorization": f"Zoho-oauthtoken {access_token}",
        "X-com-zoho-invoice-organizationid": ZOHO_ORGANIZATION_ID,
        "Content-Type": "application/json",
    }
    params = {"organization_id": ZOHO_ORGANIZATION_ID}

    response = requests.post(url, headers=headers, params=params, json=payload)
    if response.status_code not in (200, 201):
        raise Exception(f"Zoho Invoice error {response.status_code}: {response.text}")

    data = response.json()
    invoice = data.get("invoice", {})
    print(
        f"Draft invoice created in Zoho: "
        f"{invoice.get('invoice_number', '(number pending)')} "
        f"— total {INVOICE_CURRENCY_SYMBOL}{invoice.get('total', 0)}"
    )
    return invoice

# ─────────────────────────────────────────────
#  MAIN
# ─────────────────────────────────────────────

if __name__ == "__main__":
    print("Fetching Jira issues...")
    issues = fetch_issues()
    print(f"Found {len(issues)} issue(s) with time logged.")

    if not issues:
        print("No issues found for the past 7 days. No email or invoice created.")
    else:
        plain_text, html = build_report(issues)
        print("\n--- Report preview ---")
        print(plain_text)
        print("----------------------\n")

        send_email(plain_text, html)

        print("Creating draft invoice in Zoho Invoice...")
        create_zoho_invoice(issues)

        print("Done.")


# ─────────────────────────────────────────────
#  HOW TO SET UP YOUR .env FILE
# ─────────────────────────────────────────────
#
#  1. In the same folder as this script, copy .env.example to a new
#     file named exactly ".env"
#  2. Fill in every value in .env (see the sections below for where to
#     get each one — Jira token, Gmail app password, Zoho credentials)
#  3. Install python-dotenv so the script can read it:
#       pip install python-dotenv
#  4. NEVER commit .env to git or share it. Add this line to your
#     .gitignore if you keep this script in a repo:
#       .env
#
#  If you're running this on a schedule via cron or Task Scheduler
#  (not GitHub Actions), the .env file approach above works as-is —
#  just make sure .env sits next to the script on that machine.
#
#  If you're running this via GitHub Actions, don't use a .env file at
#  all. Instead add each value as a Repository Secret (Settings →
#  Secrets and variables → Actions → New repository secret), then
#  reference them in your workflow file, e.g.:
#
#    - name: Run weekly report
#      run: python jira_weekly_report.py
#      env:
#        JIRA_EMAIL: ${{ secrets.JIRA_EMAIL }}
#        JIRA_API_TOKEN: ${{ secrets.JIRA_API_TOKEN }}
#        EMAIL_FROM: ${{ secrets.EMAIL_FROM }}
#        EMAIL_TO: ${{ secrets.EMAIL_TO }}
#        EMAIL_PASSWORD: ${{ secrets.EMAIL_PASSWORD }}
#        ZOHO_CLIENT_ID: ${{ secrets.ZOHO_CLIENT_ID }}
#        ZOHO_CLIENT_SECRET: ${{ secrets.ZOHO_CLIENT_SECRET }}
#        ZOHO_REFRESH_TOKEN: ${{ secrets.ZOHO_REFRESH_TOKEN }}
#        ZOHO_ORGANIZATION_ID: ${{ secrets.ZOHO_ORGANIZATION_ID }}
#        ZOHO_CUSTOMER_ID: ${{ secrets.ZOHO_CUSTOMER_ID }}
#
# ─────────────────────────────────────────────
#  HOW TO GET YOUR JIRA API TOKEN
# ─────────────────────────────────────────────
#
#  1. Go to: https://id.atlassian.com/manage-profile/security/api-tokens
#  2. Click "Create API token"
#  3. Give it a name like "Weekly report script"
#  4. Copy the token — you'll paste it as JIRA_API_TOKEN in your .env file
#
# ─────────────────────────────────────────────
#  HOW TO GET A GMAIL APP PASSWORD
# ─────────────────────────────────────────────
#
#  Gmail blocks plain passwords for scripts. Use an App Password instead:
#  1. Go to: https://myaccount.google.com/apppasswords
#     (requires 2-factor authentication to be enabled on your Google account)
#  2. Select app: "Mail", device: "Other" → type "Jira report"
#  3. Copy the 16-character password — you'll paste it as EMAIL_PASSWORD in your .env file
#
#  Not using Gmail? Change SMTP_SERVER and SMTP_PORT:
#    Outlook / Office 365:  smtp.office365.com, port 587
#    Yahoo:                 smtp.mail.yahoo.com, port 587
#
# ─────────────────────────────────────────────
#  HOW TO SET UP THE ZOHO INVOICE API (one-time setup)
# ─────────────────────────────────────────────
#
#  Zoho uses OAuth2, so there's a bit more setup than Jira, but you only
#  do this once — after that the script refreshes its own access token.
#
#  1. Register a "Self Client" to get a client ID/secret:
#     - Go to https://api-console.zoho.com.au/   (AU data center console)
#     - Click "Add Client" → "Self Client" → Create
#     - Copy the Client ID and Client Secret into ZOHO_CLIENT_ID and
#       ZOHO_CLIENT_SECRET in your .env file
#
#  2. Generate an authorization code (in the same Self Client screen,
#     under the "Generate Code" tab):
#     - Scope:   ZohoInvoice.invoices.CREATE,ZohoInvoice.invoices.READ
#     - Duration: choose the longest option available (e.g. 10 minutes —
#       you just need it long enough to complete step 3 immediately after)
#     - Click Create, copy the generated code
#
#  3. Exchange that code for a refresh token by running this once
#     (replace the placeholders, then run from a terminal):
#
#       curl -X POST https://accounts.zoho.com.au/oauth/v2/token \
#         -d "grant_type=authorization_code" \
#         -d "client_id=YOUR_CLIENT_ID" \
#         -d "client_secret=YOUR_CLIENT_SECRET" \
#         -d "redirect_uri=https://www.zoho.com/invoice" \
#         -d "code=THE_CODE_FROM_STEP_2"
#
#     The JSON response includes a "refresh_token" — copy that into
#     ZOHO_REFRESH_TOKEN in your .env file. It does not expire (unless revoked).
#
#  4. Find your Organization ID:
#     - In Zoho Invoice, go to Settings → Organization Profile
#     - Copy the Organization ID into ZOHO_ORGANIZATION_ID in your .env file
#
#  5. Find (or create) the Customer ID for this client:
#     - In Zoho Invoice, go to Customers, open the client, and copy the
#       ID from the URL, OR fetch it via the API:
#
#       curl https://www.zohoapis.com.au/invoice/v3/contacts?organization_id=YOUR_ORG_ID \
#         -H "Zoho-oauthtoken YOUR_ACCESS_TOKEN"
#
#     Copy the matching contact_id into ZOHO_CUSTOMER_ID in your .env file.
#
#  6. Set HOURLY_RATE to your billing rate.
#
#  This script is set up for the Australian Zoho data center
#  (accounts.zoho.com.au / www.zohoapis.com.au). If you ever move
#  organizations to a different data center, update ZOHO_ACCOUNTS_DOMAIN
#  and ZOHO_API_DOMAIN in your .env file accordingly (e.g. .eu, .in, .com for US).
#
#  Note: the invoice is created as a DRAFT — it is NOT emailed to your
#  client automatically. Review it in Zoho Invoice and send it yourself.
#
# ─────────────────────────────────────────────
#  HOW TO SCHEDULE THIS WEEKLY (pick one)
# ─────────────────────────────────────────────
#
#  Mac / Linux — cron job:
#    Open terminal and run:  crontab -e
#    Add this line (runs every Monday at 8am):
#      0 8 * * 1 /usr/bin/python3 /path/to/jira_weekly_report.py
#
#  Windows — Task Scheduler:
#    1. Open Task Scheduler → Create Basic Task
#    2. Trigger: Weekly, Monday, 8:00 AM
#    3. Action: Start a program → python.exe
#    4. Arguments: C:\path\to\jira_weekly_report.py
#
#  Free cloud option — GitHub Actions:
#    Push this script to a private GitHub repo and add a workflow file
#    with "schedule: cron: '0 20 * * 0'" (Sunday 8pm UTC = Monday 8am NZT)
#    Ask Claude to generate the GitHub Actions workflow file if needed.
#    IMPORTANT: do NOT commit your .env file — use GitHub Actions
#    "Repository secrets" instead. See "HOW TO SET UP YOUR .env FILE"
#    above for the exact workflow syntax.
