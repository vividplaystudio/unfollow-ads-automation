#!/usr/bin/env python3
"""
Apple Search Ads token refresher.

Runs every 30 min via GitHub Actions.
Generates a fresh access token and writes it to Google Sheets.

Environment variables (provided as GitHub Secrets):
    CLIENT_ID
    TEAM_ID
    KEY_ID
    PRIVATE_KEY_PEM (full contents of the .pem file)
    SPREADSHEET_ID  (your Google Sheet ID)
    GOOGLE_SERVICE_ACCOUNT_JSON (service account credentials JSON)
"""

import base64
import ftplib
import hashlib
import json
import os
import ssl
import subprocess
import tempfile
import time
import urllib.parse
import urllib.request


CLIENT_ID = os.environ["CLIENT_ID"]
TEAM_ID = os.environ["TEAM_ID"]
KEY_ID = os.environ["KEY_ID"]
PRIVATE_KEY_PEM = os.environ["PRIVATE_KEY_PEM"]
SPREADSHEET_ID = os.environ["SPREADSHEET_ID"]
GOOGLE_SERVICE_ACCOUNT_JSON = os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"]
FTP_HOST = os.environ.get("FTP_HOST", "")
FTP_USER = os.environ.get("FTP_USER", "")
FTP_PASS = os.environ.get("FTP_PASS", "")
FTP_PATH = os.environ.get("FTP_PATH", "/")

# Which cell to write the token to (on the _Config tab)
TOKEN_CELL = "_Config!B1"


def base64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def sign_with_openssl(data: bytes, key_path: str) -> bytes:
    digest = hashlib.sha256(data).digest()
    result = subprocess.run(
        ["openssl", "pkeyutl", "-sign", "-inkey", key_path,
         "-pkeyopt", "digest:sha256"],
        input=digest,
        capture_output=True,
        check=True,
    )
    der_sig = result.stdout
    assert der_sig[0] == 0x30
    assert der_sig[2] == 0x02
    r_len = der_sig[3]
    r_bytes = der_sig[4:4 + r_len]
    s_start = 4 + r_len
    assert der_sig[s_start] == 0x02
    s_len = der_sig[s_start + 1]
    s_bytes = der_sig[s_start + 2:s_start + 2 + s_len]
    r_padded = r_bytes.lstrip(b"\x00").rjust(32, b"\x00")
    s_padded = s_bytes.lstrip(b"\x00").rjust(32, b"\x00")
    return r_padded + s_padded


def generate_jwt(key_path: str) -> str:
    now = int(time.time())
    expiry = now + (180 * 24 * 60 * 60)
    header = {"alg": "ES256", "kid": KEY_ID, "typ": "JWT"}
    payload = {
        "sub": CLIENT_ID,
        "aud": "https://appleid.apple.com",
        "iat": now,
        "exp": expiry,
        "iss": TEAM_ID,
    }
    h = base64url_encode(json.dumps(header, separators=(",", ":")).encode())
    p = base64url_encode(json.dumps(payload, separators=(",", ":")).encode())
    signing_input = f"{h}.{p}".encode("ascii")
    sig = sign_with_openssl(signing_input, key_path)
    return f"{h}.{p}.{base64url_encode(sig)}"


def get_access_token(jwt_token: str) -> dict:
    data = urllib.parse.urlencode({
        "grant_type": "client_credentials",
        "client_id": CLIENT_ID,
        "client_secret": jwt_token,
        "scope": "searchadsorg",
    }).encode("utf-8")
    req = urllib.request.Request(
        "https://appleid.apple.com/auth/oauth2/token",
        data=data,
        headers={
            "Host": "appleid.apple.com",
            "Content-Type": "application/x-www-form-urlencoded",
        },
        method="POST",
    )
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read().decode("utf-8"))


def get_google_access_token() -> str:
    """Get Google API access token from service account JSON using ES256-style flow (RS256)."""
    creds = json.loads(GOOGLE_SERVICE_ACCOUNT_JSON)
    private_key_pem = creds["private_key"]
    client_email = creds["client_email"]

    # Write private key to temp file
    with tempfile.NamedTemporaryFile(mode="w", suffix=".pem", delete=False) as f:
        f.write(private_key_pem)
        key_path = f.name

    now = int(time.time())
    header = {"alg": "RS256", "typ": "JWT"}
    payload = {
        "iss": client_email,
        "scope": "https://www.googleapis.com/auth/spreadsheets",
        "aud": "https://oauth2.googleapis.com/token",
        "iat": now,
        "exp": now + 3600,
    }
    h = base64url_encode(json.dumps(header, separators=(",", ":")).encode())
    p = base64url_encode(json.dumps(payload, separators=(",", ":")).encode())
    signing_input = f"{h}.{p}".encode("ascii")

    # Sign with RS256 using openssl
    result = subprocess.run(
        ["openssl", "dgst", "-sha256", "-sign", key_path],
        input=signing_input,
        capture_output=True,
        check=True,
    )
    sig = result.stdout
    os.unlink(key_path)
    jwt_token = f"{h}.{p}.{base64url_encode(sig)}"

    data = urllib.parse.urlencode({
        "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
        "assertion": jwt_token,
    }).encode("utf-8")

    req = urllib.request.Request(
        "https://oauth2.googleapis.com/token",
        data=data,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read().decode("utf-8"))["access_token"]


def write_token_to_sheet(token: str, expires_at_iso: str) -> None:
    google_token = get_google_access_token()
    url = (
        f"https://sheets.googleapis.com/v4/spreadsheets/{SPREADSHEET_ID}/values/"
        f"{urllib.parse.quote(TOKEN_CELL)}?valueInputOption=RAW"
    )
    body = json.dumps({
        "range": TOKEN_CELL,
        "values": [[token]],
    }).encode("utf-8")

    req = urllib.request.Request(
        url,
        data=body,
        headers={
            "Authorization": f"Bearer {google_token}",
            "Content-Type": "application/json",
        },
        method="PUT",
    )
    with urllib.request.urlopen(req) as resp:
        resp.read()

    # Also write expiry timestamp to B2
    url2 = (
        f"https://sheets.googleapis.com/v4/spreadsheets/{SPREADSHEET_ID}/values/"
        f"{urllib.parse.quote('_Config!B2')}?valueInputOption=RAW"
    )
    body2 = json.dumps({
        "range": "_Config!B2",
        "values": [[expires_at_iso]],
    }).encode("utf-8")
    req2 = urllib.request.Request(
        url2, data=body2,
        headers={
            "Authorization": f"Bearer {google_token}",
            "Content-Type": "application/json",
        },
        method="PUT",
    )
    with urllib.request.urlopen(req2) as resp:
        resp.read()


def main() -> None:
    # Write private key to temp file
    with tempfile.NamedTemporaryFile(mode="w", suffix=".pem", delete=False) as f:
        f.write(PRIVATE_KEY_PEM)
        key_path = f.name

    try:
        print("Generating JWT...")
        jwt_token = generate_jwt(key_path)
        print("Exchanging for access token...")
        resp = get_access_token(jwt_token)
        access_token = resp["access_token"]
        expires_in = resp["expires_in"]
        expires_at = time.strftime(
            "%Y-%m-%d %H:%M:%S UTC",
            time.gmtime(time.time() + expires_in)
        )
        print(f"Got token, expires in {expires_in}s")

        print("Writing to Google Sheet...")
        write_token_to_sheet(access_token, expires_at)
        print(f"✅ Sheet updated, token valid until {expires_at}")

        # Also push token to cPanel so the dashboard PHP proxy can use it
        if FTP_HOST:
            try:
                print("Uploading .token to cPanel...")
                ctx = ssl.create_default_context()
                ctx.check_hostname = False
                ctx.verify_mode = ssl.CERT_NONE
                ftp = ftplib.FTP_TLS(FTP_HOST, timeout=30, context=ctx)
                ftp.login(FTP_USER, FTP_PASS)
                ftp.prot_p()
                # Navigate to dashboard folder
                try:
                    ftp.cwd(FTP_PATH)
                except ftplib.error_perm:
                    pass
                # Write token as .token file
                import io
                ftp.storbinary("STOR .token", io.BytesIO(access_token.encode()))
                ftp.quit()
                print("✅ Token uploaded to cPanel")
            except Exception as e:
                print(f"⚠ FTP upload failed: {e}")
    finally:
        os.unlink(key_path)


if __name__ == "__main__":
    main()
