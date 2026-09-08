# Deploying stock-checker to the VPS

Production runs as a single-shot script under cron — cron owns the timing,
so there is no scheduler to keep alive. `run_loop.py` is for local dev only.

## Install

There is no git remote for this project yet, so the code is copied to the
box rather than cloned:

```bash
# from the dev machine
tar czf - --exclude=.venv --exclude=state --exclude=.git . \
  | ssh vps 'mkdir -p /root/stock-checker && tar xzf - -C /root/stock-checker'

ssh vps 'cd /root/stock-checker && ./deploy/setup.sh'
```

`setup.sh` is safe to re-run. It creates the venv, installs dependencies and
Chromium, creates `.env` from the example if missing, installs the logrotate
config, and **merges** its cron entries into the existing crontab — it never
replaces it, because news-notifier's live jobs share that crontab.

Then fill in the webhook:

```bash
ssh vps 'nano /root/stock-checker/.env'   # DISCORD_WEBHOOK_URL
```

## What gets scheduled

One job at `:15/:45`, running every enabled site. Those slots keep it clear
of news-notifier's Playwright jobs at `:00/:30` and `:10/:40`, so two
Chromium instances never start at once on a small box.

**It is installed dry-run** — no `--send-discord`. See below.

## Going live

The Amazon checker watches the same ASINs as news-notifier's EU pilot. If
both send to Discord you get duplicate alerts from diverging state. So the
cutover is two steps, in this order:

1. **Let it run dry for a while.** Check `deploy/status.sh` and the log.
   Confirm it resolves products, pins the delivery location on every market,
   and accumulates sane reference prices. Nothing is sent while dry.
2. **Swap the systems over.** Remove the pilot's delivery job, then add
   `--send-discord` to this one:

```bash
ssh vps "crontab -l | grep -v 'track_delivery_multi.py' | crontab -"
ssh vps "crontab -l | sed 's#run.sh main.py#run.sh main.py --send-discord#' | crontab -"
ssh vps '/root/stock-checker/deploy/status.sh'
```

`status.sh` has an explicit conflict check for exactly this and will say so
loudly if both end up live at once.

Leave `scrape_hypixel.py` alone throughout — it is unrelated and should keep
running. `scrape_catalog_multi.py` (new-product discovery) has no equivalent
here yet, so keep it too unless you no longer want those alerts.

## Checking on it

```bash
ssh vps '/root/stock-checker/deploy/status.sh'
ssh vps 'tail -40 /root/stock-checker/logs/stock-checker.log'
```

`status.sh` reports what is scheduled, the last START/END pair, recent
non-zero exits, per-site state counts, and the conflict check.

## Running by hand

`run.sh` sources `.env`; invoking Python directly does not, so load it
yourself:

```bash
ssh vps 'cd /root/stock-checker && set -a && . .env && set +a && \
  ./.venv/bin/python main.py --site popsplanet'
```

Always dry-run first against production. Adding `--send-discord` to a manual
run posts real alerts.

## Notes

- State lives in `state/` on the VPS and is not in git. Don't overwrite it
  casually — the reference prices under `state/amazon_reference_prices.json`
  are accumulated knowledge, and losing them re-opens the cold-start window
  where a scalped listing can only be reported as "price unverified".
- `run.sh` holds a per-invocation `flock`, so a slow run cannot pile up
  behind itself; a skipped run logs `SKIP` rather than failing.
- Logs rotate weekly, 4 kept, via `/etc/logrotate.d/stock-checker`.
