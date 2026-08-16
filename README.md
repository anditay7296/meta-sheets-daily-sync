# Daily Meta Ads → Google Sheets sync

Pulls yesterday's spend, leads and landing-page views from the Meta Ads API and
writes them into a Google Sheet — automatically, every night, on GitHub's
servers. Your own computer can stay switched off.

**→ [SETUP.md](SETUP.md) — start here.** About 30 minutes, one time.

## What you get

- One row per day, newest at the top, one column per ad account
- Spend, total with tax, leads, cost per lead, landing-page views, opt-in rate
- Runs at **00:06 MYT**, plus a **09:06** self-heal pass that repairs anything the
  first run missed
- Rewrites yesterday's row each run (Meta keeps adjusting figures for hours), and
  backfills any of the last three days whose row is missing
- Opens a GitHub issue if a run fails, so you find out without checking

## How it's put together

| File | Purpose |
|---|---|
| `daily_update_both_sheets.py` | the sync itself — fetch, write, verify, self-heal |
| `accounts.json` | your ad accounts and which column each one writes to |
| `.env` | your credentials, for local runs only (gitignored) |
| `.github/workflows/daily-sync.yml` | the schedule; reads credentials from repo secrets |
| `.claude/skills/meta-sheets-sync/` | operating manual — setup, checks, repairs |

Configuration lives in `accounts.json` and four repo secrets. You shouldn't need
to edit the Python.

## Two rules worth knowing

**Only ever run one scheduler.** No cron, no `launchd`, no second copy elsewhere.
Two runs writing at once shift the sheet's rows under each other and destroy data.
GitHub Actions on its own is enough.

**No formulas in data rows.** Every figure is written as a static number, so
history can never silently rewrite itself when past data changes.

## Using Claude Code

Open this repo and ask. The bundled skill handles setup, "did last night's run
work?", repairing rows, recounting leads, and swapping ad accounts.
