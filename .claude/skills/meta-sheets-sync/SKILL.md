---
name: meta-sheets-sync
description: Operate, verify and repair the daily Meta Ads → Google Sheets sync in this repo. Use for first-time setup, answering "did the sync run?", fixing sheet rows (blank rows, duplicate dates, a missing day, wrong spend or leads), swapping or adding ad accounts, and diagnosing figures that look wrong. Trigger on "did it run", "check the sync", "fix the sheet", "the numbers look wrong", "add an ad account", "set up the sync".
---

# Daily Meta Ads → Google Sheets sync

This repo pulls daily spend/leads/LPV from the Meta Ads API and writes them into a
Google Sheet, once per night via GitHub Actions. This skill is the operating manual.

## Golden rules — read before changing anything

**1. GitHub Actions is the ONLY scheduler. Never add a second one.**
No `launchd`, no `cron`, no Claude scheduled task, no copy running on another
machine. The insert-at-row-6 logic is *not* concurrency-safe: two runs firing at
once both decide to insert, and they shift rows under each other. This exact
failure destroyed a day's data in the original deployment — a blank row 6 and one
day's row overwritten. The workflow has a `concurrency:` group as a backstop, but
that only protects runs *inside this repo*. A second scheduler elsewhere defeats it.

**2. Never write a formula into a data row.**
Every value in columns C–O must be a computed static number. Formulas make
historical data mutable: a lead removed from `Sheet1` months later would silently
rewrite an old CPL. Nothing starting with `=` goes into a data row, not even
`=SUM(C6:G6)`.

**3. Guard every manual write against row shift.**
Before writing to row N, assert the date label in column B is what you expect —
and assert it *again* after the write. If a scheduled run inserts a row while your
edit is in flight, you would otherwise write into the wrong day silently:

```python
r = ws.row_values(7)
assert r[1].strip() == "Jul 8", f"Row 7 is {r[:2]}, aborting"
ws.update_cells(cells, value_input_option="USER_ENTERED")
assert ws.row_values(7)[1].strip() == "Jul 8", "Row shifted mid-write!"
```

**4. Manual runs are fine — except in the cron windows.**
Avoid 00:06–02:00 and 09:06–11:00 MYT. GitHub's scheduled runs often land 30–90
minutes late, so the window is wider than the cron time suggests. Any other hour
is safe.

## Connecting to the sheet

Every snippet below assumes this preamble:

```python
import os
from dotenv import load_dotenv
from google.oauth2.service_account import Credentials
import gspread

D = os.path.expanduser("~/meta-sheets-daily-sync")   # adjust to this repo's path
load_dotenv(os.path.join(D, ".env"))
creds = Credentials.from_service_account_file(
    os.path.join(D, os.getenv("CREDS_FILE")),
    scopes=["https://spreadsheets.google.com/feeds",
            "https://www.googleapis.com/auth/drive"])
sh = gspread.authorize(creds).open_by_key(os.getenv("SHEET_ID"))
ws = sh.worksheet("Daily Reporting")
```

## Sheet layout

Row 6 is today; each new day is inserted at row 6, pushing history down.
Rows 1–5 are headers maintained by hand.

| Col | Meaning | Source |
|-----|---------|--------|
| A, B | Day name, date (`"Jul 8"`) | computed |
| C–G | Per-account spend | Meta API, one column per account in `accounts.json` |
| H | Total spend | sum of C–G |
| I | With tax | `H × TAX_MULTIPLIER` (env, default 1.0) |
| J | Total View | manual entry — preserved on past rows; **blanked on today's row (row 6)** every run |
| K | Cost per view | `H / J` (blank on today's row, since J is blank) |
| L | Leads | unique count from `Sheet1` |
| M | CPL | `H / L` |
| N | LPV | `landing_page_view` from Meta |
| O | Opt-in rate | `L / N` (decimal; sheet formats as %) |

> **Total View (J) on today's row.** Row 6 is rebuilt from scratch each run, so J
> is written blank — there's no prior value to freeze yet, and K depends on it so
> it's blank too. When the day rolls over and that row becomes "yesterday", the
> refresh reads whatever you typed into J and preserves it. So: type Total View in
> *after* the day is done, not while it's still today's row.

## "Did the sync run?"

Check all three layers — **never trust the log alone**. A run can print
`✅ Done` and still have left the sheet corrupted, because the verification step
only checks the row it *thinks* is yesterday.

```bash
gh run list --repo <owner>/<repo> --limit 5
RUNID=$(gh run list --repo <owner>/<repo> --limit 1 --json databaseId --jq '.[0].databaseId')
gh run view $RUNID --repo <owner>/<repo> --log | \
  grep -E "Daily Update|TODAY values|YESTERDAY values|ALL CHECKS|MISMATCH|Removed blank|Inserting new row|updating in place|backfilling"
```

Then read the sheet itself and confirm rows 6/7 hold the dates you expect. Every
healthy run logs `Daily Update — <today>` and `✅ ALL CHECKS PASSED`. On a normal
day it also logs `Inserting new row at position 6` (the fresh day). `updating in
place` appears only on a *re-run* for a day already present — normal for a manual
dispatch or the 09:06 self-heal pass, but a midnight run that logs it means the
row was already there.

## Repair playbooks

### Blank rows / duplicate dates / missing day

The script self-heals most of this on its next run: `clean_blank_rows` over rows
6–15, then a date-aware scan (today at row 6, the last three days expected at rows
7–9, with a fallback lookup across rows 6–20 when a date is out of place). Only a
day that is **missing entirely** gets backfilled — a row whose date is already
present is treated as immutable history and left untouched. Prefer just running
it. Repair by hand only when the layout is too scrambled for the scan, or a
missing day is older than three days.

```python
for i in range(6, 17):
    r = ws.row_values(i)
    print(i, r[:3] if r else "BLANK")
```

Delete blank rows **bottom-up** so indices don't shift under you:

```python
blank = [i for i in range(6, 16) if not any(v.strip() for v in (ws.row_values(i) or []))]
for i in reversed(blank):
    ws.delete_rows(i)
```

To restore a missing day, **do not insert a label row first.** The script detects
a missing day only by its *absence* — a row that already carries the date in
column B is treated as immutable and never filled, so a hand-inserted label row
permanently blocks the backfill.

- **Missing day is within the last 3 days** — just run the script. It inserts the
  row at the right slot and fills it with finalized figures (the self-heal pass).
- **Missing day is older than 3 days** — the script won't reach it. Insert the row
  *and* fill it by hand in one shot (guard against row-shift as always):

  ```python
  r6 = ws.row_values(6); assert r6[1].strip() == "Jul 4"   # confirm neighbours first
  ws.insert_row(["Fri", "Jul 3"], index=7)
  # then build + write that row's cells from a Meta API fetch for Jul 3
  ```

### Figures look too low

Almost always **not a bug**: Meta reports partially during the day and keeps
settling for hours afterwards. A mid-day snapshot legitimately shows a fraction of
the final spend. Confirm against the API before "fixing" anything:

```python
import json, subprocess, urllib.parse
tr = urllib.parse.quote('{"since":"2026-07-08","until":"2026-07-08"}')
url = (f"https://graph.facebook.com/v21.0/{acct}/insights?fields=spend,actions"
       f"&time_increment=1&level=account&limit=10&time_range={tr}"
       f"&access_token={os.getenv('META_TOKEN')}")
print(json.loads(subprocess.run(["curl","-s",url],capture_output=True,text=True).stdout))
```

If the sheet really is stale, the cheapest fix is to let the next scheduled run
refresh it. Only write by hand when you need the number now.

### Leads look wrong — match all three date formats

`Sheet1` timestamps are not consistent. Counting only one format silently
undercounts. This caused a real incident: a day showed 139 leads instead of 275,
which inflated CPL from RM 52 to RM 104. Always probe all three:

```python
from datetime import date
col_a, col_d = ws1.col_values(1)[1:], ws1.col_values(4)[1:]   # ws1 = sh.worksheet("Sheet1")
d = date(2026, 7, 8)
iso, slash, padded = d.isoformat(), d.strftime("%-m/%-d/%Y"), d.strftime("%m/%d/%Y")
uniq = {col_d[i].strip() for i, a in enumerate(col_a)
        if i < len(col_d) and col_d[i]
        and ((a or "").strip().startswith(iso) or slash in a or padded in a)}
print(len(uniq))
```

Leads only ever move **up** for a past day (`max(existing, recount)`) — a lower
recount means a lookup gap, not fewer leads. After changing L, recompute M and O.

### A cell refuses to update

Known gspread quirk: `ws.update_cells()` can silently fail to overwrite a cell
that already holds a long formula. Force it directly:

```python
ws.update(values=[[value]], range_name="L7", value_input_option="USER_ENTERED")
```

## Adding, removing or swapping an ad account

Ad accounts get banned and replaced; this is routine.

1. Confirm the token can actually read the new account first:
   ```bash
   curl -s "https://graph.facebook.com/v21.0/act_<ID>?fields=name,account_status,currency&access_token=$META_TOKEN"
   ```
   `account_status: 1` means active. A token that can't see it returns an error —
   fix that before touching config, or the column silently stays 0.
2. Edit `accounts.json` (local) **and** update the `ACCOUNTS_JSON` repo secret —
   CI reads the secret, not the committed file. Changing only one is the most
   common mistake here.
3. Update the sheet's own header cells in rows 1–5 for that column (hand-maintained).
4. **"Starting today" means no backfill.** Past rows keep the old account's
   history. Write today's value into the current row and recompute H, I, N, and
   any dependent ratios for that row only.

The verification step derives its account list from `accounts.json`, so it stays
in sync automatically — no second place to edit.

## First-time setup

Follow `SETUP.md`. Ordering that matters: the sheet must be shared with the
service-account email *before* the first run, and all four secrets
(`META_TOKEN`, `SHEET_ID`, `GOOGLE_CREDS_JSON`, `ACCOUNTS_JSON`) must exist or the
workflow fails fast with an explicit message naming the missing ones.

`GOOGLE_CREDS_JSON` is **base64-encoded**; `ACCOUNTS_JSON` is **plain JSON**.
Pasting the Google key as raw JSON is the classic setup failure — it arrives
empty and the run dies with `JSONDecodeError: Expecting value: line 1 column 1`.
