# Run the dashboard pullers on cPanel (no more GitHub Actions throttling)

Once set up, your dashboard refreshes **every 15 min for Meta + Adjust** and **every 30 min for RC** — guaranteed, no GitHub flakiness.

## One-time setup (~15 min)

### 1. Pick a folder on your cPanel host
A standard place to put scripts is `~/unfollow-ads/`. From cPanel Terminal:

```bash
mkdir -p ~/unfollow-ads && cd ~/unfollow-ads
```

### 2. Upload the files
Copy these files from this repo into `~/unfollow-ads/`:
- `refresh_meta_ads.py`
- `refresh_adjust.py`
- `refresh_dashboard_json.py` (only if you want the slow RC refresh here too)
- `cpanel/run.sh`
- `cpanel/config.sh.example`

You can either:
- Use cPanel File Manager (drag & drop)
- Use `scp` over SSH: `scp refresh_*.py cpanel/run.sh cpanel/config.sh.example user@genivox.com:~/unfollow-ads/`
- Or `git clone` the repo into a tmp dir then copy

### 3. Make config.sh from the template
```bash
cd ~/unfollow-ads
cp config.sh.example config.sh
nano config.sh         # fill in your real tokens
chmod 600 config.sh    # IMPORTANT: only you should be able to read it
chmod +x run.sh
```

The important values to fill in:
- `LOCAL_OUTPUT_DIR` — the actual path to your dashboard folder. To find it: `cd` into the dashboard folder via cPanel File Manager / SSH and run `pwd`.
- `META_ACCESS_TOKEN`, `ADJUST_API_TOKEN`, `REVENUECAT_API_KEY` — same tokens you put in GitHub Secrets. View them from GitHub repo → Settings → Secrets (you can re-generate if forgotten).

### 4. Test manually
```bash
~/unfollow-ads/run.sh meta
```
Then check `~/unfollow-ads/cron.log` for errors and verify `meta_ads.json` was updated in your dashboard folder.

If it worked, run the other two:
```bash
~/unfollow-ads/run.sh adjust
~/unfollow-ads/run.sh rc      # only if you moved this one too
```

### 5. Add cron jobs in cPanel

cPanel → **Cron Jobs** → Add:

| Schedule | Command |
|---|---|
| Every 15 min: `*/15 * * * *` | `bash ~/unfollow-ads/run.sh meta` |
| Every 15 min: `*/15 * * * *` | `bash ~/unfollow-ads/run.sh adjust` |
| Every 15 min: `*/15 * * * *` | `bash ~/unfollow-ads/run.sh store` |
| Nightly: `20 3 * * *` | `bash ~/unfollow-ads/run.sh store-cold` |

**Do not also schedule `rc`.** It is the legacy full refresh, kept only as a
fallback. `store` replaces it and does the same job in seconds.

---

## Migrating an existing install to the fact store

The old `rc` job re-derived every number from the RevenueCat API on each run,
so its runtime tracked *total install history* rather than recent activity: 15
minutes in May, 71 minutes by August at 111k customers. Once it exceeded its
own 30-minute interval, runs overlapped and raced on `data.json`, which is how
the dashboard came to be serving 2026-07-01 data in the middle of August.

The store fixes that by keeping facts instead of re-fetching them. Cost becomes
proportional to what happened since last time.

### One-time cutover

**1. Re-run the installer.** GitHub → Actions → **Install cPanel cron setup**
→ Run workflow. It ships `store.py`, the adapter, the three ingest scripts, and
the Apple Ads credentials that were previously missing.

**2. Build the store.** GitHub → Actions → **Store Backfill** → Run workflow.

This needs no shell access: everything the backfill reads is reachable over
HTTP or an API, so it runs on the GitHub runner and uploads the finished
`store.db` to `~/unfollow-ads/` over FTP. It resumes from whatever store is
already on the host, so it is safe to run more than once.

Inputs:

| Input | Default | Meaning |
|---|---|---|
| `ad_lookback_days` | 90 | How much ad-spend history to load |
| `attr_batch` | 40000 | Cap on attribution lookups; `0` skips the customer walk |
| `fresh` | false | Ignore the existing store and rebuild from empty |

The customer walk inside it takes roughly as long as one old `rc` run. That is
the last time that walk ever blocks anything.

**If you would rather do it on the host** (cPanel → Terminal), the same thing:

```bash
bash ~/unfollow-ads/run.sh store-backfill
tail -40 ~/unfollow-ads/cron.log
```

### 4. Then swap the cron

Replace the `*/30 ... run.sh rc` job with the two `store` jobs above.

### Verifying

```bash
tail -30 ~/unfollow-ads/cron.log
```

A healthy hot-path run looks like:

```
[rc] read 8014 log records (0 unparseable)
[rc] wrote 8014 txn, 13 attributed customers, 5023 sub_state
[rc] store now: 8014 txn 2026-08-10..2026-08-20 | gross $43,307.30 -> proceeds $32,971.66
[ads] apple: 112 keyword-days 2026-08-14..2026-08-20 (16 keywords)
--- RevenueCat (fact store) ---
  111351 customers, 11017 paying, 6602 active  (0.4s, zero API calls)
```

If you instead see `--- Fetching RevenueCat (legacy API walk) ---`, the store
is empty or unreadable and the script has fallen back to the old path. Run the
backfill and check `STORE_PATH` in `config.sh`.

### Turning off the GitHub Actions refresh

Once cPanel is running the store jobs, **disable the `Refresh Dashboard`
workflow** (GitHub → Actions → Refresh Dashboard → ⋯ → Disable workflow).
Leaving it on means two independent writers publishing `data.json` on
different schedules, which is a second source of the same clobbering problem
the store was built to end.

Replace `~/unfollow-ads/` with the full absolute path (cron sometimes doesn't expand `~`). To get it: `cd ~/unfollow-ads && pwd`.

### 6. Turn off the GitHub workflows (optional)
Once cPanel is reliable, you can disable the GitHub workflows so they stop burning Actions minutes:
- GitHub repo → Actions → click each workflow → "Disable workflow"
- Or just delete `.github/workflows/refresh-dashboard.yml`, `fast-refresh.yml`, `refresh-token.yml`

## Verifying it's working

Check the log:
```bash
tail -50 ~/unfollow-ads/cron.log
```

Or check the file timestamps:
```bash
ls -la /path/to/dashboard/folder/*.json
```

The "Last update" timestamp on your dashboard should now refresh every 15 min like clockwork.
