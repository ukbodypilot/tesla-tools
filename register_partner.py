#!/usr/bin/env python3
"""One-off: register your app's domain (config.toml `domain`) with Tesla, in your region.

Reads client_id / client_secret from ~/.config/tesla/. Never prints the secret or tokens.
Tesla then fetches https://<domain>/.well-known/appspecific/com.tesla.3p.public-key.pem.
"""
import json
import pathlib
import sys
import urllib.error
import urllib.parse
import urllib.request

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import config  # noqa: E402

CFG = config.CFG_DIR
AUTH_URL = config.TOKEN_URL
API = config.CONFIG["api"]
DOMAIN = config.CONFIG["domain"]
SCOPES = "openid vehicle_device_data vehicle_location vehicle_cmds vehicle_charging_cmds"


def call(req):
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")
        try:
            return e.code, json.loads(body)
        except ValueError:
            return e.code, {"raw": body[:500]}


def main():
    if not DOMAIN:
        sys.exit("set domain = \"you.github.io\" in ~/.config/tesla/config.toml first")
    client_id = (CFG / "client_id").read_text().strip()
    secret = (CFG / "client_secret").read_text().strip()

    form = urllib.parse.urlencode({
        "grant_type": "client_credentials",
        "client_id": client_id,
        "client_secret": secret,
        "scope": SCOPES,
        "audience": API,
    }).encode()
    code, tok = call(urllib.request.Request(AUTH_URL, data=form, method="POST"))
    if code != 200 or "access_token" not in tok:
        tok.pop("access_token", None)
        print(f"partner token FAILED: HTTP {code} {tok}")
        sys.exit(1)
    print(f"partner token OK (expires_in={tok.get('expires_in')})")
    auth = {"Authorization": f"Bearer {tok['access_token']}",
            "Content-Type": "application/json"}

    code, res = call(urllib.request.Request(
        f"{API}/api/1/partner_accounts", method="POST", headers=auth,
        data=json.dumps({"domain": DOMAIN}).encode()))
    print(f"register {DOMAIN}: HTTP {code} {json.dumps(res)[:600]}")

    code, res = call(urllib.request.Request(
        f"{API}/api/1/partner_accounts/public_key?domain={DOMAIN}", headers=auth))
    print(f"lookup public_key: HTTP {code} {json.dumps(res)[:400]}")
    sys.exit(0 if code == 200 else 1)


if __name__ == "__main__":
    main()
