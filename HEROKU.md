# Heroku deployment

This branch makes the bot Heroku-ready. Runtime state lives in **Heroku
Postgres** instead of `data/*.json` and `data/*.csv`, so the bot survives
dyno restarts.

## What changed vs. `main`

| Concern | `main` | Heroku branch |
|---|---|---|
| Secrets | `config.yaml` only | Env vars first, `config.yaml` fallback |
| Rules / relay maps | `data/*.json` | Postgres tables |
| Usage state + log | `data/usage_state.json`, `data/usage.log` | Postgres tables |
| Per-user counts | `data/user_query_hist.csv` | `user_usage` table |
| Translation log | `data/translation_msg.csv` | `translation_msg` table |
| Live-edit workflow | Edit the CSV file | `UPDATE translation_msg SET text=...` |
| Process model | `python pies_translator_OPENAI.py` | `Procfile` worker dyno |

`profile/desc.json` and `profile/ABBR_MAP.json` stay on disk — they ship
with the code and are not runtime state.

## One-time setup

```bash
# 0. Make sure the Heroku CLI is logged in.
heroku login

# 1. Create the app (or reuse an existing one).
heroku create your-app-name
heroku git:remote -a your-app-name

# 2. Provision Postgres. essential-0 is fine for this bot.
heroku addons:create heroku-postgresql:essential-0
# DATABASE_URL is now set automatically by the addon.

# 3. Set the secrets.
heroku config:set DISCORD_TOKEN='your-discord-bot-token'
heroku config:set OPENAI_API_KEY='sk-...'

# Optional knobs (defaults shown):
heroku config:set OPENAI_MODEL='gpt-4o-mini'
heroku config:set PIES_DEBUG='0'
heroku config:set FLAG_EPHEMERAL_SECONDS='60'
heroku config:set OPENAI_BUDGET_DOLLARS_PER_DAY='5.00'

# 4. Push this branch.
git push heroku claude/heroku-deployment-setup-eV6oX:main

# 5. Import existing data/ contents into Postgres.
heroku run python migrate_data_to_db.py
# Safe to re-run; rules/relays use UPSERT, usage_log is cleared+reinserted,
# user_usage is overwritten.

# 6. Scale the worker dyno.
heroku ps:scale worker=1 web=0

# 7. Tail logs to confirm.
heroku logs --tail
```

You should see something like:

```
... | INFO | Postgres pool initialized (size 1-5).
... | INFO | === PieTrans BOT_VERSION = ... ===
... | INFO | Logged in as <bot> (id=...)
... | INFO | Translation log: Postgres table `translation_msg`
```

## Updating the live-edit workflow

Before: edit `data/translation_msg.csv`, save, the bot detects mtime change
and patches the Discord message.

Now: same idea, just against Postgres.

```bash
# Option 1: psql interactively.
heroku pg:psql
> UPDATE translation_msg SET text = '新译文' WHERE msg_id = 1234567890;

# Option 2: Heroku Dataclips (web UI, share read-only or editable URLs).
```

The watcher polls every 3 seconds; the message updates a few seconds later.

## Operations

### Logs

```bash
heroku logs --tail
heroku logs --tail --source app --dyno worker
```

### Restart

```bash
heroku restart
```

### Inspect data

```bash
heroku pg:psql
> \dt
> SELECT * FROM rules LIMIT 5;
> SELECT * FROM usage_state;
> SELECT window_label, tokens_used FROM usage_log ORDER BY created_at DESC LIMIT 10;
```

### Back up

Heroku Postgres essential plans include point-in-time recovery up to several
days; for explicit snapshots:

```bash
heroku pg:backups:capture
heroku pg:backups:download   # downloads latest.dump
```

### Scaling

Discord bots are single-process — keep `worker=1`. If you need to redeploy
without the bot going down for a few seconds, that's a Heroku limitation
(brief restart) more than a bot issue.

## Local development

Same code, point `DATABASE_URL` at a local Postgres:

```bash
# Quick local Postgres:
docker run --rm -d --name pies-pg -e POSTGRES_PASSWORD=postgres -p 5432:5432 postgres:16

# config.yaml (gitignored) or env vars:
export DATABASE_URL='postgresql://postgres:postgres@127.0.0.1:5432/postgres?sslmode=disable'
export DISCORD_TOKEN='your-token'
export OPENAI_API_KEY='sk-...'

python migrate_data_to_db.py   # one-time data import
python pies_translator_OPENAI.py
```

You can also keep secrets in `config.yaml` locally; env vars win when both
are set.

## Schema reference

`db.py` creates these tables on first connect:

- `rules (channel_id, language, flag, link_channel_id)`
- `relay_map (src_msg_id, target_channel_id, target_msg_id)`
- `relay_origin (relayed_msg_id, origin_channel_id)`
- `relay_reverse (relayed_msg_id, src_msg_id, src_channel_id)`
- `usage_state` — single-row daily budget snapshot
- `usage_log (window_label, tokens_used, created_at)`
- `user_usage (username, query_counts)`
- `translation_msg (msg_id, ..., text, created_at, updated_at)` — `updated_at`
  is bumped automatically on `UPDATE`, which triggers the live-edit watcher.

## Troubleshooting

- **`Missing DATABASE_URL`** — the Postgres addon wasn't provisioned, or you
  ran the bot outside Heroku without setting the env var.
- **`ssl: WRONG_VERSION_NUMBER`** — Heroku Postgres requires SSL; `db.py`
  enables it automatically when it detects the Heroku environment (`DYNO`
  env var or a known managed-host marker in the URL). If you connect to a
  local Postgres without SSL, append `?sslmode=disable` to the URL.
- **`asyncpg.exceptions.InvalidPasswordError`** — Heroku rotates DB
  credentials occasionally; `DATABASE_URL` is updated automatically, but
  you'd need to redeploy/restart the worker to pick up the change.
- **Bot stays offline after deploy** — check `heroku ps`. If no worker
  dyno is running, `heroku ps:scale worker=1`.
- **CSV edits no longer work** — they don't, by design. Edit
  `translation_msg` in Postgres instead (see "Live-edit workflow" above).
