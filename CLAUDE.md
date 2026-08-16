# Project rules — daily Meta Ads → Google Sheets sync

Operating manual, repair playbooks and setup guidance live in
`.claude/skills/meta-sheets-sync/SKILL.md`. Read it before changing behaviour.
This file holds the rules that must never be broken.

## Configuration lives in config, not code

Ad accounts come from `accounts.json` (`{"Name": ["act_<id>", "<column letter>"]}`);
credentials come from `.env` locally and repo secrets in CI. Don't reintroduce
hardcoded account ids or sheet ids into `daily_update_both_sheets.py`.

In CI, `accounts.json` is written from the `ACCOUNTS_JSON` secret — a change to
the committed file alone does not affect scheduled runs. Update both.

## 🔒 Never write formulas into data rows

Columns C–O must always be exact computed numbers. Nothing starting with `=`,
not even `=SUM(C6:G6)`.

Formulas make historical data mutable — a lead deleted from `Sheet1` months later
would silently rewrite an old CPL. All history must stay immutable static values.

## 🕐 One scheduler only

GitHub Actions is the sole scheduler. Never add `launchd`, `cron`, a Claude
scheduled task, or a second copy on another machine.

The insert-at-row-6 logic is not concurrency-safe: simultaneous runs both insert
and shift rows under each other. This destroyed a day's data in the original
deployment. The workflow's `concurrency:` group only protects runs within the
repo — an external scheduler defeats it.

## ✍️ Guard manual writes against row shift

Assert the target row's date label in column B before writing **and** after.
A scheduled run inserting mid-edit would otherwise send your write to the wrong
day silently.

## 🔍 Verify against the API, not the log

A run can log `✅ Done` and still leave the sheet wrong — verification only checks
the row it believes is yesterday. When asked "did it run", check all three:
the workflow run status, the log contents, and the actual sheet rows.

Meta reports partially during the day and keeps settling for hours. Figures that
look low mid-day are usually correct-for-now, not a bug — confirm against the API
before "fixing" them.

## 🔑 Never commit secrets

`.env`, `*.json` service-account keys, and `accounts.json` are gitignored
(`accounts.json.example` is the committed template). Keep it that way. When
staging, add named files — never `git add .`.
