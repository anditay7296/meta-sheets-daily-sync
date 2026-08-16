# Setup guide

One-time setup, about 30 minutes. At the end you'll have a Google Sheet that
updates itself every night with yesterday's Meta Ads numbers — no computer of
yours needs to be switched on.

You'll create **your own** credentials throughout. Nothing here depends on
anyone else's tokens, and nobody else's setup breaks if you change yours.

> **Using Claude Code?** Open this repo and say *"set up this sync"*. The bundled
> skill walks you through these same steps and does the fiddly parts for you.
> The manual instructions below work fine on their own too.

---

## Step 1 — Copy the Google Sheet

Make your own copy of the sheet template you were given
(**File → Make a copy** in Google Sheets).

From your copy's URL, grab the long id between `/d/` and `/edit`:

```
https://docs.google.com/spreadsheets/d/1AbCdEfGhIjKlMnOpQrStUvWxYz.../edit
                                       └────────── this is your SHEET_ID ──────────┘
```

Keep it handy. The sheet must have a tab named exactly **`Daily Reporting`**, and
a **`Sheet1`** tab holding your leads (dates in column A, a lead identifier such
as phone number in column D).

## Step 2 — Create a Google service account

This is the robot account that writes to your sheet.

1. Go to [console.cloud.google.com](https://console.cloud.google.com) and create a
   project (any name).
2. **APIs & Services → Library** — enable both **Google Sheets API** and
   **Google Drive API**.
3. **IAM & Admin → Service Accounts → Create service account**. Any name; no
   optional roles needed. Create it.
4. Open the new service account → **Keys → Add key → Create new key → JSON**.
   A `.json` file downloads. **This file is a password — never commit it.**
5. Copy the service account's email — it looks like
   `something@your-project.iam.gserviceaccount.com`.

## Step 3 — Share your sheet with the service account

In your sheet: **Share** → paste the service-account email → give it **Editor** →
send. Untick "notify people" (it's a robot).

Skipping this is the single most common setup failure — the run fails with a
permission error even though everything else is correct.

## Step 4 — Create a Meta access token

Use a **System User** token. Unlike a personal token it doesn't expire every
60 days, so your sync won't quietly die two months from now.

1. [business.facebook.com](https://business.facebook.com) → **Business Settings**.
2. **Users → System Users → Add** — name it e.g. `sheet-sync`, role *Employee*.
3. **Assign assets → Ad Accounts** — select every ad account you want in the
   sheet, grant at least **View performance**.
4. **Generate new token** → pick your app → tick the **`ads_read`** permission →
   generate. Copy it now; it's shown once.

Verify it can see each account (repeat per account id):

```bash
curl -s "https://graph.facebook.com/v21.0/act_YOUR_ID?fields=name,account_status&access_token=YOUR_TOKEN"
```

You want a name back and `"account_status":1`. An error here means the token
can't read that account — fix it now, otherwise that column silently stays 0.

## Step 5 — Create your repo from this template

Click **Use this template → Create a new repository** at the top of this repo's
GitHub page. Make it **private** — the config describes your ad accounts.

## Step 6 — Decide your account → column mapping

Columns **C, D, E, F, G** hold one ad account each. Write your mapping as JSON:

```json
{
  "My Main Account": ["act_1234567890123456", "C"],
  "Client A":        ["act_2345678901234567", "D"],
  "Client B":        ["act_3456789012345678", "E"]
}
```

Names are labels for your own benefit — use anything. Only the account ids and
column letters matter. You don't have to fill all five columns, but **C–G is the
limit: at most five accounts.** Columns A–B and H–O are computed by the script
(day, date, totals, leads, landing-page views) and can't hold an ad account.

## Step 7 — Add the secrets

In your new repo: **Settings → Secrets and variables → Actions → New repository
secret**. Add these four:

| Secret | Value |
|---|---|
| `META_TOKEN` | the token from step 4 |
| `SHEET_ID` | the id from step 1 |
| `GOOGLE_CREDS_JSON` | the step-2 JSON key, **base64-encoded** (see below) |
| `ACCOUNTS_JSON` | the JSON from step 6, **plain, not encoded** |

Optional — add this only if your sheet's "with tax" column should differ from the
plain total:

| Secret | Value |
|---|---|
| `TAX_MULTIPLIER` | number column I multiplies spend by; default `1.0` (no adjustment). E.g. `1.08` for 8% tax |

`GOOGLE_CREDS_JSON` must be base64 — pasting raw JSON is the classic mistake. The
run then fails at the **Create Google credentials file** step with a `base64:
invalid input` error, before Python even starts. Encode it first:

```bash
base64 -i ~/Downloads/your-key.json | tr -d '\n' | pbcopy
```

(On Linux: `base64 -w0 ~/Downloads/your-key.json`.)

Or set all four from your terminal with the GitHub CLI. Each `gh secret set`
without a value prompts you to paste the secret, so paste the matching value from
the steps above when asked:

```bash
gh secret set META_TOKEN --repo OWNER/REPO       # paste step-4 token
gh secret set SHEET_ID --repo OWNER/REPO         # paste step-1 sheet id
gh secret set ACCOUNTS_JSON --repo OWNER/REPO    # paste step-6 JSON
base64 -i ~/Downloads/your-key.json | tr -d '\n' | gh secret set GOOGLE_CREDS_JSON --repo OWNER/REPO
```

## Step 8 — Test it

**Actions** tab → *Daily Meta Ads → Google Sheet Sync* → **Run workflow**.

It takes about a minute. A green tick and these lines in the log mean success:

```
Daily Update — 2026-08-16
📌 TODAY values: H=..., L=..., N=...
✅ ALL CHECKS PASSED for 2026-08-15
```

Then open your sheet: row 6 should hold today, row 7 yesterday, with per-account
spend across C–G. **You're done** — it now runs every night by itself.

If the run failed, the log names the cause directly. The three usual ones:

| Log says | Fix |
|---|---|
| `Missing repo secret(s): ...` | add the named secret (step 7) |
| `base64: invalid input` at *Create Google credentials file* | `GOOGLE_CREDS_JSON` wasn't base64-encoded (step 7) |
| `PermissionError` / `403` from Google | sheet not shared with the service account (step 3) |
| `accounts.json: "…" column must be one of C, D, E, F, G` | fix `ACCOUNTS_JSON` — accounts only go in columns C–G (step 6) |

---

## How it runs

Twice daily: **00:06 MYT** for the main pass, and **09:06 MYT** as a self-heal
pass that repairs anything the midnight run missed. Each run writes today's row,
rewrites yesterday's row (Meta keeps adjusting figures for hours after midnight),
and backfills any of the last three days whose row is missing entirely. If a run
fails, it opens a GitHub issue in your repo so you find out without checking.

Scheduled runs on GitHub commonly start 30–90 minutes late. That's normal and
costs nothing — the run corrects whatever it finds.

> ### ⚠️ Only ever run one scheduler
>
> Don't add a cron job, a `launchd` job, or a second copy of this on another
> machine. Two runs writing at once shift the sheet's rows under each other — in
> the original deployment this blanked one row and destroyed a day's data before
> anyone noticed. GitHub Actions alone is enough; it doesn't need your computer.
>
> Running it manually is fine, just avoid 00:06–02:00 and 09:06–11:00 MYT.

## Changing accounts later

Ad accounts get banned and replaced — routine. Update **both** your local
`accounts.json` **and** the `ACCOUNTS_JSON` repo secret (CI reads the secret,
not the committed file), then relabel that column's header cells in the sheet.
Past rows keep the old account's history; the new account starts from today.

## Running it on your own machine (optional)

You don't need this — GitHub does the work. It's occasionally handy for testing.

```bash
pip install gspread google-auth python-dotenv
cp .env.example .env                      # fill in your values
cp accounts.json.example accounts.json    # fill in your accounts
mv ~/Downloads/your-key.json ./gcp-creds.json
python3 daily_update_both_sheets.py
```

`.env`, `accounts.json` and `*.json` keys are gitignored — keep it that way.
