# Invoice Automated Script

Path: `zoho-jira-invoicing/invoice_automated.py`

## Overview

`invoice_automated.py` is a small automation script that:

- Collects time logged by the current user in Jira for the last 7 days,
- Sends a formatted weekly time report by email (plain text + HTML),
- Creates a DRAFT invoice in Zoho Invoice where each Jira issue becomes an invoice line (quantity = hours logged).

The script reads all secrets and configuration from environment variables (optionally via a local `.env` file if you install `python-dotenv`).

## Requirements

- Python 3.7+
- Packages:
  - `requests`
  - `python-dotenv` (optional, for loading `.env`)
- SMTP access for sending email (server, port, app password)
- Jira API access (email + API token) with permissions to read issues/worklogs
- Zoho Invoice OAuth credentials (client id, client secret, refresh token) and organization/customer IDs

Install deps:

```sh
pip install requests python-dotenv
```

## Top-level behavior (what the script does)

1. Fetches issues in `JIRA_PROJECT` where `worklogAuthor = currentUser()` and `worklogDate` is in the last 7 days.
2. For each issue, fetches that issue's worklogs and sums the time that the current user logged in the date range.
3. Builds a plain-text and HTML report and emails it to `EMAIL_TO`.
4. Creates a DRAFT invoice in Zoho Invoice with one line per Jira issue (rate = `HOURLY_RATE`, quantity = hours logged).

If no issues are found in the 7-day window the script exits without sending email or creating an invoice.

## Configuration / Environment variables

All sensitive values must be set via environment variables. You can copy `.env.example` -> `.env` and fill it, and optionally install `python-dotenv` to have the script load that file.

Required / used environment variables:

- Jira
  - `JIRA_BASE_URL` — base URL for your Jira instance (e.g. `https://yourcompany.atlassian.net`)
  - `JIRA_PROJECT` — project key used for the JQL
  - `JIRA_EMAIL` — email address for API auth (the script compares worklog author to this)
  - `JIRA_API_TOKEN` — Jira API token (used with HTTP Basic auth)

- Email (SMTP)
  - `SMTP_SERVER` — SMTP host (e.g. `smtp.gmail.com`)
  - `SMTP_PORT` — SMTP port (integer; e.g. `587`)
  - `EMAIL_FROM` — From address used to log in/send
  - `EMAIL_TO` — Recipient(s) (comma-separated if necessary)
  - `EMAIL_PASSWORD` — SMTP password (app password if using Gmail)

- Zoho Invoice
  - `ZOHO_ACCOUNTS_DOMAIN` — OAuth token URL domain (e.g. `https://accounts.zoho.com`)
  - `ZOHO_API_DOMAIN` — Zoho API base domain (e.g. `https://invoice.zoho.com`)
  - `ZOHO_CLIENT_ID`
  - `ZOHO_CLIENT_SECRET`
  - `ZOHO_REFRESH_TOKEN`
  - `ZOHO_ORGANIZATION_ID`
  - `ZOHO_CUSTOMER_ID` — the customer to whom the invoice will be assigned

- Other / optional
  - `HOURLY_RATE` — invoice rate per hour (float; default `0.00`)
  - `INVOICE_CURRENCY_SYMBOL` — currency symbol used for printing totals (default `$`)

Example `.env` snippet:

```env
JIRA_BASE_URL=https://yourcompany.atlassian.net
JIRA_PROJECT=MYPROJ
JIRA_EMAIL=you@example.com
JIRA_API_TOKEN=XXXXXXXXXXXX

SMTP_SERVER=smtp.gmail.com
SMTP_PORT=587
EMAIL_FROM=you@example.com
EMAIL_TO=client@example.com
EMAIL_PASSWORD=your-smtp-app-password

ZOHO_ACCOUNTS_DOMAIN=https://accounts.zoho.com
ZOHO_API_DOMAIN=https://invoice.zoho.com
ZOHO_CLIENT_ID=your_client_id
ZOHO_CLIENT_SECRET=your_client_secret
ZOHO_REFRESH_TOKEN=your_refresh_token
ZOHO_ORGANIZATION_ID=123456789
ZOHO_CUSTOMER_ID=987654321

HOURLY_RATE=150.00
INVOICE_CURRENCY_SYMBOL=$
```

## Important functions (summary & signatures)

- require_env(name)
  - Ensures a required env var is present; exits with a helpful error if missing.

- fetch_worklogs_for_issue(issue_key, since_date, until_date, auth, headers) -> total_seconds
  - Calls Jira issue worklog endpoint and returns the sum of `timeSpentSeconds` for worklogs:
    - authored by `JIRA_EMAIL`
    - whose `started` date is between `since_date` and `until_date` (YYYY-MM-DD)

- fetch_issues() -> list[issue]
  - Builds a JQL search (worklogAuthor = currentUser() and date range = last 7 days),
  - pages search results,
  - for each returned issue, calls `fetch_worklogs_for_issue` and keeps only issues with time in-range.
  - Attaches the in-range seconds into `issue["fields"]["timespent"]` for downstream use.

- seconds_to_hours(seconds) -> str
  - Formats seconds to a string like `"2.50h"`.

- build_report(issues) -> (plain_text, html)
  - Produces both a plain-text and HTML representation of the weekly time report. Includes total time and issue rows.

- send_email(plain_text, html)
  - Uses `smtplib.SMTP` with STARTTLS to authenticate with `EMAIL_FROM` / `EMAIL_PASSWORD` and send the message.

- get_zoho_access_token() -> access_token
  - Exchanges `ZOHO_REFRESH_TOKEN`, `ZOHO_CLIENT_ID`, `ZOHO_CLIENT_SECRET` for a short-lived Zoho access token.

- build_zoho_line_items(issues) -> list[line_item]
  - Creates a Zoho Invoice `line_items` payload: each item has `name` (issue key), `description` (summary), `rate` (HOURLY_RATE), and `quantity` (hours rounded to 2 decimals).

- create_zoho_invoice(issues) -> invoice
  - Calls Zoho Invoice API to create a draft invoice with `line_items`. Prints created invoice number/total.

## How dates / windowing work

- The script calculates:
  - since = now - 6 days
  - until = now
  - So it covers a 7-day inclusive window ending today.
- Worklog `started` field is truncated to the date (`YYYY-MM-DD`) and compared inclusive.
  Running the script manually
  From the project directory:

```sh
# ensure .env exists or export environment variables
python zoho-jira-invoicing/invoice_automated.py
```

The script will:

- print a fetch message,
- print a preview of the plain-text report,
- send the email and print confirmation,
- create a draft Zoho invoice and print invoice number/total.

## Scheduling (example)

To run this weekly via cron (Sun night / Mon morning for a week ending Sunday), add a crontab entry, for example to run every Monday at 08:00:

1. Edit the crontab:

```sh
crontab -e
```

2. Example entry (adjust the python path and working dir as needed):

```
0 8 * * MON /usr/bin/env bash -lc 'cd /path/to/zoho-jira-invoicing && /usr/bin/python3 invoice_automated.py >> /path/to/logs/jira_weekly.log 2>&1'
```

If you rely on `.env`, ensure the cron environment loads it (e.g., wrap execution with `export $(cat .env | xargs)` _careful_ with secrets) or use system/CI secret storage and set env vars in the job.

## Troubleshooting & common errors

- Missing environment variables:
  - The script will exit (via `require_env`) if required envs are missing or if you rely on a missing value. Confirm `.env` is present and loaded or that environment variables are exported in your shell/cron/CI.
- Jira auth errors (401/403) or API errors:
  - Verify `JIRA_EMAIL` and `JIRA_API_TOKEN` are correct and user has permission to read worklogs.
  - Check `JIRA_BASE_URL` and project key.
- Zoho auth errors:
  - `get_zoho_access_token` will raise on non-200 responses. Confirm `ZOHO_REFRESH_TOKEN`, `ZOHO_CLIENT_ID`, `ZOHO_CLIENT_SECRET` are valid.
  - If you need to generate a refresh token, follow Zoho OAuth 2.0 flow in their docs (generate refresh token once; store it securely).
- SMTP / email errors:
  - Check SMTP host/port, username/password, and whether the account requires app-specific password or security settings (e.g., Gmail requires an app password or OAuth).
- Time zone & date mismatch:
  - The script truncates the Jira `started` field and compares based on server `now()`. If your Jira instance or app uses a different timezone, verify worklog dates match expected window.
- Zoho invoice posting errors:
  - Verify `ZOHO_ORGANIZATION_ID` and `ZOHO_CUSTOMER_ID` are correct. API error responses include helpful text—inspect them for missing/invalid fields.

## Security & best practice notes

- NEVER commit `.env` or credentials into source control.
- Treat `EMAIL_PASSWORD` and `ZOHO_REFRESH_TOKEN` as secrets; use your system/CI secret manager where possible.
- Consider rotating Zoho refresh tokens / API tokens periodically.
- For production, prefer using OAuth flows that avoid storing passwords; for email, consider using a dedicated service or OAuth for SMTP if available.
