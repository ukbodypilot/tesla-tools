"""Install-specific settings, read from ~/.config/tesla/config.toml (outside the repo).

Everything here is optional; see config.example.toml. Runtime tunables (polling intervals,
budget, alert thresholds, units, home electricity rate) live in the database instead and are
edited on the web page's Settings tab.
"""
from __future__ import annotations

import os
import pathlib
import tomllib

HERE = pathlib.Path(__file__).resolve().parent
CFG_DIR = pathlib.Path(os.environ.get("TESLA_CONFIG_DIR", pathlib.Path.home() / ".config" / "tesla"))

REGIONS = {
    "na": "https://fleet-api.prd.na.vn.cloud.tesla.com",
    "eu": "https://fleet-api.prd.eu.vn.cloud.tesla.com",
}
TOKEN_URL = "https://fleet-auth.prd.vn.cloud.tesla.com/oauth2/v3/token"
AUTHORIZE_URL = "https://auth.tesla.com/oauth2/v3/authorize"

DEFAULTS = {
    "vin": "",                 # blank = the first car on the account (looked up once, then cached)
    "region": "na",            # na | eu
    "name": "",                # display name; blank = the car's own name
    "domain": "",              # your app's origin domain, e.g. you.github.io (register_partner.py)
    "redirect_uri": "http://localhost:8080/callback",
    "listen": "0.0.0.0:9877",  # web page + /metrics
    "public_url": "",          # how you reach the page, used in links (e.g. http://192.168.1.10:9877/)
    "away_link_file": "",      # optional: radio-gateway short-links JSON, for an "away from home" link in emails
    "away_link_path": "/tesla/",   # which entry of that file is this page
    "grafana_url": "",         # optional link from the page header
    "data_dir": "",            # blank = <repo>/data
    "places_db": "",           # optional: a Family Locator famloc.db, for place names ("Home")
    "home_rate_label": "home", # label for the home-electricity comparison, e.g. your utility's name
    "email": {},               # see config.example.toml
    "apps": [],                # optional links to sibling apps for the header's Apps menu: [{name, url}]
}


def load() -> dict:
    cfg = {k: (dict(v) if isinstance(v, dict) else v) for k, v in DEFAULTS.items()}
    path = CFG_DIR / "config.toml"
    if path.exists():
        with path.open("rb") as f:
            user = tomllib.load(f)
        for k, v in user.items():
            if k not in DEFAULTS:
                raise ValueError(f"{path}: unknown setting {k!r}")
            cfg[k] = v
    if cfg["region"] not in REGIONS:
        raise ValueError(f"region must be one of {sorted(REGIONS)}")
    cfg["api"] = REGIONS[cfg["region"]]
    cfg["data_dir"] = pathlib.Path(cfg["data_dir"]).expanduser() if cfg["data_dir"] else HERE / "data"
    cfg["places_db"] = pathlib.Path(cfg["places_db"]).expanduser() if cfg["places_db"] else None
    host, _, port = cfg["listen"].rpartition(":")
    cfg["listen_addr"] = (host or "0.0.0.0", int(port))
    return cfg


CONFIG = load()
