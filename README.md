# Unfollow Tracker — Ads Automation

Automated Apple Search Ads token refresh for the ads dashboard.

## How it works

GitHub Actions runs `refresh_token.py` every 30 minutes. The script:
1. Generates a JWT using your private key
2. Exchanges it for a fresh ASA access token
3. Writes the token to a Google Sheet cell (`_Config!B1`)

The dashboard (Google Apps Script) reads the token from that cell.

## GitHub Secrets required

- `ASA_CLIENT_ID` — Apple Search Ads Client ID
- `ASA_TEAM_ID` — Apple Search Ads Team ID
- `ASA_KEY_ID` — Apple Search Ads Key ID
- `ASA_PRIVATE_KEY_PEM` — full contents of your `private-key.pem` file
- `SPREADSHEET_ID` — Google Sheet ID where the dashboard lives
- `GOOGLE_SERVICE_ACCOUNT_JSON` — Google service account JSON with Sheets edit access

## Manual trigger

GitHub → Actions tab → Refresh Apple Search Ads Token → "Run workflow"
