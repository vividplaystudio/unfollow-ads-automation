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
| Every 30 min: `*/30 * * * *` | `bash ~/unfollow-ads/run.sh rc` |

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
