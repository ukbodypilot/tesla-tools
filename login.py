#!/usr/bin/env python3
"""Tesla user login (region from config.toml).

  login.py url              -> print the page to log in at
  login.py exchange <URL>   -> swap the code in the redirected URL for tokens,
                               saved to ~/.config/tesla/tokens.json (600). Prints no tokens.
"""
import json
import os
import pathlib
import secrets
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import config  # noqa: E402

CFG = config.CFG_DIR
AUTHZ = config.AUTHORIZE_URL
TOKEN = config.TOKEN_URL
API = config.CONFIG["api"]
REDIRECT = config.CONFIG["redirect_uri"]
SCOPES = ("openid offline_access user_data vehicle_device_data vehicle_location "
          "vehicle_cmds vehicle_charging_cmds")


def client_id():
    return (CFG / "client_id").read_text().strip()


def make_url():
    state = secrets.token_urlsafe(16)
    (CFG / "login_state").write_text(state)
    q = urllib.parse.urlencode({
        "response_type": "code", "client_id": client_id(), "redirect_uri": REDIRECT,
        "scope": SCOPES, "state": state, "prompt_missing_scopes": "true",
    })
    print(f"{AUTHZ}?{q}")


def exchange(url):
    qs = urllib.parse.parse_qs(urllib.parse.urlparse(url.strip()).query)
    if "error" in qs:
        sys.exit(f"Tesla returned error: {qs.get('error')} {qs.get('error_description')}")
    if "code" not in qs:
        sys.exit("no ?code= in that URL")
    want = (CFG / "login_state").read_text().strip()
    if qs.get("state", [""])[0] != want:
        sys.exit("state mismatch — use the URL from the latest login link")
    form = urllib.parse.urlencode({
        "grant_type": "authorization_code", "client_id": client_id(),
        "client_secret": (CFG / "client_secret").read_text().strip(),
        "code": qs["code"][0], "redirect_uri": REDIRECT, "audience": API,
    }).encode()
    try:
        with urllib.request.urlopen(urllib.request.Request(TOKEN, data=form), timeout=30) as r:
            tok = json.loads(r.read())
    except urllib.error.HTTPError as e:
        sys.exit(f"token exchange FAILED: HTTP {e.code} {e.read().decode(errors='replace')[:400]}")
    tok["obtained_at"] = int(time.time())
    path = CFG / "tokens.json"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(tok, f)
    (CFG / "login_state").unlink(missing_ok=True)
    print(f"tokens saved: access expires_in={tok.get('expires_in')}s, "
          f"refresh_token={'yes' if tok.get('refresh_token') else 'NO'}, scopes={tok.get('scope')}")


if __name__ == "__main__":
    if sys.argv[1:2] == ["url"]:
        make_url()
    elif sys.argv[1:2] == ["exchange"] and len(sys.argv) == 3:
        exchange(sys.argv[2])
    else:
        sys.exit(__doc__)
