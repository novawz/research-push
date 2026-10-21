# Research Digest App Guide

App URL: `https://research-push.streamlit.app/`

This app discovers new papers by journals/keywords, deduplicates results, and builds a readable daily digest. You can also push results to Slack.

## 1. How to Use the App

1. Open the app and go to `⚙ Settings`.
2. Select journals you want to follow (multi-select supported).
3. Add keywords (optional) and exclude keywords (for example, `survey`).
4. Choose a time range (today, last 7 days, or custom).
5. Generate/refresh to get results.
6. Review outputs in:
   - `Today Feed`: papers in the current time window
   - `Worth Reading`: prioritized picks (more complete when AI is enabled)

## 2. (Optional) Enable AI Summary

1. Go to `⚙ Settings`.
2. Turn on **Use ChatGPT API**.
3. Enter your **Session API Key**.
4. Choose a model and save.

Without an API key, the app still works, but AI-specific parts are hidden or downgraded.

## 3. Use Slack Push in Web App

### 3.1 Prepare a Slack Webhook

1. Visit `https://api.slack.com/apps`
2. Create a Slack app and enable **Incoming Webhooks**.
3. Add it to a target channel, then copy the webhook URL (`https://hooks.slack.com/services/...`).

### 3.2 Connect Webhook in the App

1. Go back to `⚙ Settings` in the app.
2. Turn on **Enable Webhook Push**.
3. Paste your webhook URL.
4. Save settings.
5. After generating a digest, click **Push to Webhook**.

### 3.3 What Gets Pushed

- Paper items in your current digest (with links)
- Worth Reading summary (when available)

## 4. Troubleshooting

- No papers found: widen the time range and reduce keyword filters.
- Slack received nothing: check webhook URL validity and channel permissions.
- Webhook leaked: revoke/rotate it in Slack immediately, then update the app.

## 5. Automatic Push Operations

GitHub Actions checks for due subscriptions every five minutes (`2/5 * * * *`).
Each subscription keeps its saved local time, timezone, and daily/Monday schedule.
The workflow allows delivery up to 60 minutes after the saved time, within the
same local calendar day, to tolerate GitHub scheduling delays. Other callers retain the 15-minute default unless they
set `AUTO_PUSH_GRACE_MINUTES`. GitHub schedules can be delayed or dropped, and
public-repository schedules can be disabled after 60 days without activity.

Set the existing `AUTO_PUSH_DATABASE_URL` GitHub secret to the subscription
database. No GitHub PAT is needed for native scheduling. After deploying this
workflow, disable the old `auto_push_dispatch_each_minute` Supabase cron job
(the removal SQL is in `supabase/sql/remove_auto_push_cron.sql`) and revoke its
dispatch PAT. Do not install the legacy `setup_auto_push_cron.sql` for this setup.

The workflow also sets this project's Supabase reference and IPv4 session pooler
host. The connection helper only rewrites a matching direct Supabase hostname;
an already matching project/session-pooler connection stays on that target.
It preserves the existing password and database options, requires the `postgres`
database and port 5432, and uses TLS (`require` or an existing stricter mode).
Mismatched projects, usernames, and alternate libpq routes are rejected before
connecting. Callers with neither override setting keep their original DSN;
setting only one is a configuration error.

Manual workflow runs default to **check_only**, which executes:

```sh
python daily_push.py --check
```

`--check` (also `--dry-run`) verifies that the existing subscription table is
readable and prints only the number currently due. It uses a read-only database
connection, does not create the table, claim subscriptions, fetch papers, or
send webhooks. Missing schema, missing configuration, and database failures exit
nonzero. PostgreSQL connections have a 10-second connection timeout, a
15-second statement timeout, and a 5-second lock timeout.

Scheduled runs and legacy repository dispatches process due subscriptions.
Uncheck **check_only** only when a manual delivery is intended. Each subscription
is claimed immediately before processing, after applying `--limit`; a process
interruption cannot claim later subscriptions it has not started. An interruption
during a delivery can still leave that particular subscription marked for the
day, so check delivery evidence before manually resetting its status.

Run the offline regression tests with:

```sh
python -m unittest discover -s tests -v
```
