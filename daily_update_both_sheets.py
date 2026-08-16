#!/usr/bin/env python3
"""
Daily Meta Ads → Google Sheets sync.
Always inserts a new row at position 6 (newest first) with today's data,
then re-refreshes the previous days so late-settling Meta figures are corrected.

Configure via .env (META_TOKEN, CREDS_FILE, SHEET_ID) and accounts.json.
See SETUP.md for first-time setup.
"""

import json
import os
import subprocess
import time
import urllib.parse
from datetime import date, timedelta
from dotenv import load_dotenv
import gspread
from google.oauth2.service_account import Credentials

# ── Config ────────────────────────────────────────────────────────────────────
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(SCRIPT_DIR, ".env"))

def _require(name):
    val = os.getenv(name)
    if not val:
        raise SystemExit(
            f"❌ {name} is not set. Copy .env.example to .env and fill it in "
            f"(locally), or add {name} to your GitHub repo secrets (CI). See SETUP.md."
        )
    return val

CREDS_FILE = os.path.join(SCRIPT_DIR, _require("CREDS_FILE"))
META_TOKEN = _require("META_TOKEN")
SHEET_ID = _require("SHEET_ID")

# Columns C–G (1-based 3–7) are the per-account spend block. A, B and H–O are
# computed by the script (day, date, totals, leads, LPV), so an ad account can
# only live in one of these five columns.
ACCOUNT_COLS = range(3, 8)   # C, D, E, F, G
_COL_LETTERS = {c: chr(ord("A") + c - 1) for c in ACCOUNT_COLS}

# Column I = total spend × this multiplier ("with tax"). Override via the
# TAX_MULTIPLIER env var / repo secret; default 1.0 = no adjustment.
_tax_raw = os.getenv("TAX_MULTIPLIER", "1.0")
try:
    TAX_MULTIPLIER = float(_tax_raw)
except ValueError:
    raise SystemExit(f"❌ TAX_MULTIPLIER must be a number, got {_tax_raw!r}. See SETUP.md.")

# Where column L (Leads) comes from:
#   "sheet1" — unique lead ids counted from the Sheet1 tab (needs that tab)
#   "meta"   — lead actions reported by the Meta API (no extra tab needed)
LEADS_SOURCE = os.getenv("LEADS_SOURCE", "sheet1").strip().lower()
if LEADS_SOURCE not in ("sheet1", "meta"):
    raise SystemExit(
        f'❌ LEADS_SOURCE must be "sheet1" or "meta", got "{LEADS_SOURCE}". '
        f'Use "meta" if your sheet has no Sheet1 tab. See SETUP.md.'
    )


def load_accounts():
    """Load the ad-account → column mapping from accounts.json.

    Format: {"Display Name": ["act_1234567890", "C"], ...}
    Column is the sheet letter; it is converted to a 1-based index here so the
    rest of the script (and gspread.Cell) can use it directly.
    """
    path = os.path.join(SCRIPT_DIR, "accounts.json")
    if not os.path.exists(path):
        raise SystemExit(
            "❌ accounts.json not found. Copy accounts.json.example to accounts.json "
            "and list your ad accounts. See SETUP.md."
        )
    try:
        with open(path) as f:
            raw = json.load(f)
    except json.JSONDecodeError as e:
        raise SystemExit(f"❌ accounts.json is not valid JSON: {e}")

    if not raw:
        raise SystemExit("❌ accounts.json is empty — list at least one ad account.")

    accounts = {}
    for name, entry in raw.items():
        if not (isinstance(entry, list) and len(entry) == 2):
            raise SystemExit(
                f'❌ accounts.json entry for "{name}" must be ["act_<id>", "<column letter>"].'
            )
        act_id, col_letter = entry
        col_letter = str(col_letter).strip().upper()
        if not act_id.startswith("act_"):
            raise SystemExit(f'❌ accounts.json: "{name}" account id must start with "act_".')
        if col_letter not in _COL_LETTERS.values():
            raise SystemExit(
                f'❌ accounts.json: "{name}" column must be one of C, D, E, F, G '
                f'(got "{col_letter}"). Columns A–B and H–O are computed by the script '
                f'(day, date, totals, leads, LPV) and cannot hold an ad account, so at '
                f'most five accounts are supported.'
            )
        accounts[name] = (act_id, ord(col_letter) - ord("A") + 1)

    cols = [c for _, c in accounts.values()]
    if len(set(cols)) != len(cols):
        raise SystemExit("❌ accounts.json: two accounts are mapped to the same column.")
    return accounts


ACCOUNTS = load_accounts()

TAB_NAME = "Daily Reporting"
DAY_NAMES = ["Mon","Tue","Wed","Thu","Fri","Sat","Sun"]

# ── API helper ────────────────────────────────────────────────────────────────

def api_get(url, retries=5):
    for attempt in range(retries):
        result = subprocess.run(["curl", "-s", url], capture_output=True, text=True)
        data = json.loads(result.stdout)
        if "error" in data and data["error"].get("code") in [17, 80000, 4]:
            wait = 60 * (attempt + 1)
            print(f"   ⏳ Rate limit hit, waiting {wait}s...")
            time.sleep(wait)
            continue
        return data
    return data

def fetch_one_day(ad_account, day_str):
    """Fetch one day of account-level data."""
    time_range = urllib.parse.quote(f'{{"since":"{day_str}","until":"{day_str}"}}')
    url = (f"https://graph.facebook.com/v21.0/{ad_account}/insights"
           f"?fields=spend,actions"
           f"&time_increment=1&level=account&limit=10"
           f"&time_range={time_range}"
           f"&access_token={META_TOKEN}")
    data = api_get(url)
    if "error" in data:
        print(f"  API error for {ad_account}: {data['error']['message']}")
        return {"spend": 0, "leads": 0, "lpv": 0}
    rows = data.get("data", [])
    if not rows:
        return {"spend": 0, "leads": 0, "lpv": 0}
    row = rows[0]
    spend = float(row.get("spend", 0))
    lpv = 0
    lead_actions = {}
    for action in row.get("actions", []):
        at = action["action_type"]
        val = int(float(action["value"]))
        if "lead" in at.lower():
            lead_actions[at] = val
        if at == "landing_page_view":
            lpv += val

    # Meta reports the SAME conversions under several overlapping action types
    # (e.g. "lead", "onsite_web_lead", "offsite_conversion.fb_pixel_lead" all
    # returning 139). Summing them multiplies the real count, so prefer the
    # canonical "lead" total and otherwise take the max — never the sum.
    if "lead" in lead_actions:
        leads = lead_actions["lead"]
    elif lead_actions:
        leads = max(lead_actions.values())
    else:
        leads = 0

    return {"spend": spend, "leads": leads, "lpv": lpv}

def count_sheet1_leads(sh, day):
    """Count unique leads in Sheet1 for a given date.
    Replicates: countuniqueifs(Sheet1!D$2:D, Sheet1!A$2:A, ">="&day, Sheet1!A$2:A, "<"&day+1)
    Returns 0 if Sheet1 tab doesn't exist or any error occurs.
    """
    try:
        ws1 = sh.worksheet("Sheet1")
    except gspread.WorksheetNotFound:
        return 0
    try:
        col_a = ws1.col_values(1)[1:]   # dates, skip header
        col_d = ws1.col_values(4)[1:]   # lead identifiers, skip header
    except Exception as e:
        print(f"   ⚠️  Could not read Sheet1: {e}")
        return 0

    day_str = day.strftime("%-m/%-d/%Y")
    day_str_alt = day.strftime("%m/%d/%Y")
    day_iso = day.isoformat()

    unique = set()
    for i, a in enumerate(col_a):
        if i >= len(col_d):
            break
        a_stripped = a.strip() if a else ""
        # Match by any common date format
        matched = False
        if a_stripped.startswith(day_iso) or day_str in a_stripped or day_str_alt in a_stripped:
            matched = True
        if matched and col_d[i]:
            unique.add(col_d[i].strip())
    return len(unique)

def leads_for_day(sh, day, account_data):
    """Lead count for `day` from the configured source (see LEADS_SOURCE).

    "meta" reuses the lead actions already fetched by fetch_one_day, so it costs
    no extra API calls and needs no Sheet1 tab. Note the two sources count
    different things: Sheet1 counts unique lead ids, Meta counts lead actions.
    """
    if LEADS_SOURCE == "meta":
        return sum(d.get("leads", 0) for d in account_data.values())
    return count_sheet1_leads(sh, day)

def get_gc():
    scopes = ["https://spreadsheets.google.com/feeds",
              "https://www.googleapis.com/auth/drive"]
    creds = Credentials.from_service_account_file(CREDS_FILE, scopes=scopes)
    return gspread.authorize(creds)

# ── Sheet update ──────────────────────────────────────────────────────────────

def build_row_cells(row_num, day_name, date_str_display, account_data, total_lpv,
                    frozen_leads=None, frozen_j=None, sheet1_leads=None):
    """Build cell updates for a given row number — ALL exact values, NO formulas.

    Every column is written as a computed number, never a formula. This ensures
    that historical data can never change due to formula dependencies (e.g. if
    contacts are removed from Sheet1).

    For yesterday's row, `frozen_leads` (read from sheet before overwrite) takes
    priority. For today's row, `sheet1_leads` can be passed in if Sheet1 was
    counted; otherwise defaults to 0.
    """
    cells = [
        gspread.Cell(row_num, 1, day_name),                                     # A: Day
        gspread.Cell(row_num, 2, date_str_display),                             # B: Date
    ]
    mapped_cols = set()
    for name, (account_id, col) in ACCOUNTS.items():
        day_data = account_data.get(name, {})
        spend = day_data.get("spend", 0)
        cells.append(gspread.Cell(row_num, col, round(spend, 2) if spend > 0 else ""))
        mapped_cols.add(col)
    # Blank any account column no longer mapped (e.g. an account removed from
    # accounts.json) so its old spend doesn't linger while H excludes it.
    for col in ACCOUNT_COLS:
        if col not in mapped_cols:
            cells.append(gspread.Cell(row_num, col, ""))

    r = row_num
    total_spend = sum(
        account_data.get(name, {}).get("spend", 0) for name in ACCOUNTS
    )

    # H: Total spend (exact value)
    h_val = round(total_spend, 2) if total_spend > 0 else ""
    # I: With tax (exact value)
    i_val = round(total_spend * TAX_MULTIPLIER, 2) if total_spend > 0 else ""

    # J: Total View — keep existing frozen value (or blank for today)
    j_val = frozen_j if frozen_j and frozen_j > 0 else ""
    # K: Cost per view = H/J (exact)
    if j_val and total_spend > 0:
        k_val = round(total_spend / j_val, 2)
    else:
        k_val = ""

    # L: Leads (exact) — yesterday uses frozen_leads, today uses sheet1_leads or 0
    if frozen_leads is not None:
        leads_val = frozen_leads if frozen_leads > 0 else ""
    else:
        leads_val = sheet1_leads if sheet1_leads else ""

    # M: CPL = H/L (exact)
    leads_num = leads_val if isinstance(leads_val, int) and leads_val > 0 else 0
    if leads_num > 0 and total_spend > 0:
        cpl_val = round(total_spend / leads_num, 2)
    else:
        cpl_val = "-"

    # N: LPV (exact)
    lpv_val = total_lpv if total_lpv > 0 else ""

    # O: Optin rate = L/N (exact)
    if leads_num > 0 and total_lpv > 0:
        optin_val = round(leads_num / total_lpv, 4)  # stored as decimal, sheet formats as %
    else:
        optin_val = "-"

    cells.extend([
        gspread.Cell(r, 8,  h_val),      # H: Total spend
        gspread.Cell(r, 9,  i_val),      # I: With tax
        gspread.Cell(r, 10, j_val),     # J: Total View
        gspread.Cell(r, 11, k_val),     # K: Cost per view
        gspread.Cell(r, 12, leads_val), # L: Leads
        gspread.Cell(r, 13, cpl_val),   # M: CPL
        gspread.Cell(r, 14, lpv_val),   # N: LPV
        gspread.Cell(r, 15, optin_val), # O: Optin rate
        # P-T: ignored (manual entry)
    ])

    label = "YESTERDAY" if frozen_leads is not None else "TODAY"
    print(f"   📌 {label} values: H={h_val}, I={i_val}, J={j_val}, K={k_val}, "
          f"L={leads_val}, M={cpl_val}, N={lpv_val}, O={optin_val}")
    return cells

def fetch_all_accounts(day_str):
    """Fetch one day of data from all accounts. Returns (account_data, total_lpv)."""
    account_data = {}
    total_lpv = 0
    for name, (account_id, col) in ACCOUNTS.items():
        print(f"   Fetching {name} ({day_str})...")
        account_data[name] = fetch_one_day(account_id, day_str)
        d = account_data[name]
        total_lpv += d["lpv"]
        print(f"      spend={d['spend']}, leads={d['leads']}, lpv={d['lpv']}")
    return account_data, total_lpv

def clean_blank_rows(ws, start=6, end=15):
    """Delete any blank rows in the data range (top-down scan, delete bottom-up)."""
    blank = []
    for i in range(start, end + 1):
        r = ws.row_values(i)
        if not r or all(v.strip() == '' for v in r):
            blank.append(i)
    for i in reversed(blank):
        ws.delete_rows(i)
        print(f"   🧹 Removed blank row {i}")

def find_row_by_date(ws, display, start=6, end=20):
    """Find the row number whose column B equals `display` (e.g. "Jul 3"), or None."""
    col_b = ws.col_values(2)
    for r in range(start, min(end, len(col_b)) + 1):
        if col_b[r - 1].strip() == display:
            return r
    return None

def update_sheet(gc, today):
    """Insert today's row at position 6, refresh yesterday with finalized data,
    and backfill any of the last 3 days that is missing entirely."""
    print(f"\n📋 Updating '{TAB_NAME}' sheet...")
    sh = gc.open_by_key(SHEET_ID)
    ws = sh.worksheet(TAB_NAME)

    today_display = today.strftime("%b %-d")   # e.g. "Mar 29"
    today_day = DAY_NAMES[today.weekday()]

    # --- Step 0: Clean up any stray blank rows before inserting ---
    clean_blank_rows(ws)

    # --- Step 1: Insert today's row at position 6 ---
    existing_row6 = ws.row_values(6)
    if len(existing_row6) >= 2 and existing_row6[1].strip() == today_display:
        print(f"   ⚠️  Row 6 already has {today_display}, updating in place...")
    else:
        print(f"   Inserting new row at position 6 for {today_day} {today_display}...")
        ws.insert_row([], index=6)
    time.sleep(1)

    # Fetch today's data (may be partial at 00:06 AM)
    print(f"\n📊 Fetching TODAY's data ({today.isoformat()})...")
    today_data, today_lpv = fetch_all_accounts(today.isoformat())
    today_sheet1_leads = leads_for_day(sh, today, today_data)
    today_cells = build_row_cells(6, today_day, today_display, today_data, today_lpv,
                                  sheet1_leads=today_sheet1_leads)

    # --- Step 2: Refresh yesterday + backfill any missing recent day ---
    # Self-healing: don't assume yesterday sits exactly at row 7. Scan the top
    # rows by date; refresh yesterday wherever it actually is, and if any of
    # the last 3 days is missing entirely (missed run, corrupted/deleted row),
    # insert it and fill it with finalized data. All exact values, no formulas.
    yesterday = today - timedelta(days=1)
    yesterday_row = None
    yest_data = None
    extra_cells = []
    # True while rows 6..(6+k-1) hold exactly their expected dates. Inserting a
    # backfill row is only safe then — otherwise an insert would shift rows we
    # already built cell updates for.
    chain_ok = True

    for k in range(1, 4):  # yesterday, 2 days ago, 3 days ago
        day = today - timedelta(days=k)
        day_display = day.strftime("%b %-d")
        day_name = DAY_NAMES[day.weekday()]
        expected_row = 6 + k

        row_vals = ws.row_values(expected_row)
        row_date = row_vals[1].strip() if len(row_vals) >= 2 else ""
        if row_date == day_display:
            found_row = expected_row
        else:
            found_row = find_row_by_date(ws, day_display)
            if found_row:
                chain_ok = False

        if found_row:
            if k == 1:
                yesterday_row = found_row
                print(f"\n📊 Refreshing YESTERDAY's data ({day.isoformat()}) in row {found_row}...")
                vals = ws.row_values(found_row)

                # Read current J and Leads from the sheet (may be stale or manually set)
                j_val_str = vals[9] if len(vals) >= 10 else "0"
                try:
                    frozen_j = int(float(j_val_str.replace(",", "")))
                except (ValueError, TypeError):
                    frozen_j = 0

                sheet_leads_str = vals[11] if len(vals) >= 12 else "0"
                try:
                    sheet_leads = int(float(sheet_leads_str.replace(",", "")))
                except (ValueError, TypeError):
                    sheet_leads = 0

                # Fetch first — the "meta" leads source reads from this data
                yest_data, yest_lpv = fetch_all_accounts(day.isoformat())

                # Recount leads for yesterday from the configured source
                counted_leads = leads_for_day(sh, day, yest_data)

                # Prefer the higher value (the recount reflects the full day, and the
                # sheet cell may have been manually corrected higher — take whichever
                # is bigger to avoid undercounting)
                frozen_leads = max(sheet_leads, counted_leads)
                print(f"   📖 Yesterday source: J={frozen_j}, sheet_L={sheet_leads}, "
                      f"{LEADS_SOURCE}_count={counted_leads} → using {frozen_leads}")

                extra_cells += build_row_cells(found_row, day_name, day_display, yest_data, yest_lpv,
                                               frozen_leads=frozen_leads, frozen_j=frozen_j)
            # k >= 2 and present — historical row, leave untouched (immutable)
            continue

        if not chain_ok:
            print(f"   ⚠️  {day_display} not found but rows above are out of order — "
                  f"skipping backfill, fix layout manually")
            continue

        # Day missing entirely → insert + fill with finalized data at its slot
        print(f"\n🩹 {day_display} missing — backfilling at row {expected_row}...")
        ws.insert_row([], index=expected_row)
        time.sleep(1)
        fill_data, fill_lpv = fetch_all_accounts(day.isoformat())
        fill_leads = leads_for_day(sh, day, fill_data)
        extra_cells += build_row_cells(expected_row, day_name, day_display, fill_data, fill_lpv,
                                       frozen_leads=fill_leads, frozen_j=0)
        if k == 1:
            yesterday_row = expected_row
            yest_data = fill_data

    # --- Step 3: Batch update all cells ---
    all_cells = today_cells + extra_cells
    ws.update_cells(all_cells, value_input_option="USER_ENTERED")
    # A, B + full C–G account block (mapped + blanks) + H..O
    cells_per_row = 10 + len(ACCOUNT_COLS)
    print(f"   ✅ Row 6 (today) + {len(extra_cells) // cells_per_row} recent row(s) updated")

    # --- Step 4: Verify yesterday's data by reading back from sheet ---
    if yest_data is not None and yesterday_row is not None:
        time.sleep(2)
        verify_yesterday(ws, yesterday, yest_data, yesterday_row)

# ── Verification ─────────────────────────────────────────────────────────────

def verify_yesterday(ws, yesterday, api_data, row_num=7):
    """Read back yesterday's row from the sheet and compare against Meta API data."""
    print(f"\n🔍 VERIFYING yesterday's row (row {row_num})...")
    row7 = ws.row_values(row_num)
    yesterday_display = yesterday.strftime("%b %-d")

    # Confirm date matches
    sheet_date = row7[1].strip() if len(row7) >= 2 else "?"
    if sheet_date != yesterday_display:
        print(f"   ❌ MISMATCH: Row {row_num} date is '{sheet_date}', expected '{yesterday_display}'")
        return False

    ok = True

    # Verify spend per account
    # 0-based row-list index for each account's column, derived from ACCOUNTS
    # so the mapping never drifts out of sync with accounts.json.
    account_cols = {name: col - 1 for name, (_, col) in ACCOUNTS.items()}
    for name, idx in account_cols.items():
        api_spend = api_data.get(name, {}).get("spend", 0)
        sheet_val_str = row7[idx] if len(row7) > idx else ""
        try:
            sheet_spend = float(sheet_val_str.replace(",", "")) if sheet_val_str else 0
        except ValueError:
            sheet_spend = 0

        if abs(api_spend - sheet_spend) > 0.05:
            print(f"   ❌ {name}: Sheet={sheet_spend}, API={api_spend} — MISMATCH!")
            ok = False
        else:
            print(f"   ✅ {name}: {sheet_spend}")

    # Verify total spend (col H, index 7)
    api_total = sum(api_data.get(n, {}).get("spend", 0) for n in account_cols)
    sheet_total_str = row7[7] if len(row7) > 7 else ""
    try:
        sheet_total = float(sheet_total_str.replace(",", "").replace("RM", ""))
    except (ValueError, AttributeError):
        sheet_total = 0
    if abs(api_total - sheet_total) > 0.10:
        print(f"   ❌ Total Spend: Sheet={sheet_total}, API={api_total} — MISMATCH!")
        ok = False
    else:
        print(f"   ✅ Total Spend: RM{sheet_total}")

    # Verify Leads (col L, index 11) and CPL (col M, index 12) are exact values (not formulas)
    leads_str = row7[11] if len(row7) > 11 else ""
    cpl_str = row7[12] if len(row7) > 12 else ""
    print(f"   📌 Leads={leads_str}, CPL={cpl_str} (frozen values)")

    # Verify J (col J, index 9) and K (col K, index 10) are exact values
    j_str = row7[9] if len(row7) > 9 else ""
    k_str = row7[10] if len(row7) > 10 else ""
    print(f"   📌 J={j_str}, K={k_str} (frozen values)")

    # Verify LPV (col N, index 13)
    api_lpv = sum(api_data.get(n, {}).get("lpv", 0) for n in account_cols)
    sheet_lpv_str = row7[13] if len(row7) > 13 else ""
    try:
        sheet_lpv = int(float(sheet_lpv_str.replace(",", ""))) if sheet_lpv_str else 0
    except ValueError:
        sheet_lpv = 0
    if api_lpv != sheet_lpv:
        print(f"   ❌ LPV: Sheet={sheet_lpv}, API={api_lpv} — MISMATCH!")
        ok = False
    else:
        print(f"   ✅ LPV: {sheet_lpv}")

    if ok:
        print(f"   ✅ ALL CHECKS PASSED for {yesterday_display}")
    else:
        print(f"   ⚠️  SOME CHECKS FAILED — review above")
    return ok

# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    today = date.today()
    print("=" * 55)
    print(f"  Daily Update — {today.isoformat()}")
    print("=" * 55)

    gc = get_gc()
    update_sheet(gc, today)

    print(f"\n✅ Done — {today.isoformat()}!")

if __name__ == "__main__":
    main()
