#!/usr/bin/env python3
"""Tesla poller for one car: history in SQLite, Prometheus metrics, email alerts.

Sleep-safe by design:
  * Wakes the car ONLY when it has not been read for STALE_WAKE_AFTER (default 24 h, 0 = never):
    one wake_up, one read, then silence so it can sleep again.
  * The routine check is GET /vehicles/{vin}, answered by Tesla's servers, not the car.
  * The car itself (vehicle_data) is only read once it has been online for two checks in a
    row, i.e. it is staying awake on its own (driving / charging / Sentry), never during a
    brief self-wake. Parked-and-awake is re-read at most every PARKED_REREAD.
  * A spend guard (MONTHLY_BUDGET, paced per day) keeps usage under Tesla's $10/month
    personal credit, so the bill is $0. Failed wakes are never retried early.
"""
from __future__ import annotations

import datetime as dt
import gzip
import json
import logging
import os
import pathlib
import queue
import smtplib
import sys
import sqlite3
import threading
import time
import tomllib
import urllib.error
import urllib.parse
import urllib.request
from email.mime.text import MIMEText
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))  # -I drops the script dir
import charging  # noqa: E402
import config  # noqa: E402
import extras  # noqa: E402
import supercharger  # noqa: E402
import webapp  # noqa: E402

CONF = config.CONFIG
CFG = config.CFG_DIR                       # tokens.json, client_id, client_secret, private key
DATA = CONF["data_dir"]
DB_PATH = DATA / "tesla.db"
API = CONF["api"]
TOKEN_URL = config.TOKEN_URL
VIN = CONF["vin"]                          # blank: resolved from the account at startup
METRICS_ADDR = CONF["listen_addr"]         # web page + /metrics
GRAFANA_URL = CONF["grafana_url"]

STATE_EVERY = 30 * 60        # cloud-only "asleep or online?" check (~$3/month if Tesla bills it)
DRIVING_EVERY = 2 * 60       # car reads while in gear
CHARGING_EVERY = 10 * 60     # car reads while charging
PARKED_REREAD = 3 * 3600     # parked but staying awake anyway (e.g. Sentry)
STALE_WAKE_AFTER = 24 * 3600 # wake + read once if there has been no reading for this long (0 = never)
WAKE_WAIT = 10 * 60          # give a woken car this long to come online, then give up
ONLINE_STREAK_NEEDED = 2     # consecutive online state checks before we touch the car
# Tesla gives each developer account a $10/month credit: stay under it and the bill is $0.
MONTHLY_BUDGET = 8.00        # estimated $ per calendar month; polling stops when reached
PRICE = {"vehicle": 1 / 500, "vehicle_data": 1 / 500, "charging_history": 1 / 500, "wake_up": 1 / 50}
NEARBY_EVERY = 3600          # nearby Superchargers: at most hourly, and only when a read shows the car awake  # $ per call, Tesla's list prices

LOW_BATTERY_PCT = 20
LOW_BATTERY_HOUR = 21        # local hour after which an unplugged low battery is emailed
TYRE_LOW_BAR = 2.7           # ~39 psi (Model X placard is 42 psi / 2.9 bar)
TYRE_SPREAD_BAR = 0.35       # one tyre this far below the highest
PACK_KWH = 95.0              # usable pack size for the efficiency estimate (2019 X 100D)
HOME_RATE = 0.24             # home electricity $/kWh, for "what if we had charged at home"
HOME_LOSS_PCT = 10.0         # wall-to-battery loss charging at home: wall kWh = battery kWh / (1 - loss)
ALERT_ON = {"unlocked": True, "tyre": True, "battery": True, "car_alert": True, "budget": True, "broken": True}
UNITS = {"distance": "mi", "pressure": "psi", "temp": "F"}

# Settings the web app may change: name -> (kind, min, max, scale to stored unit, label, group, help).
# Stored in the settings table and applied by overwriting these module globals (all are read at
# call time, so a change takes effect on the next loop).
SETTINGS = {
    "STATE_EVERY": ("num", 10, 120, 60, "Asleep/awake check every (min)", "Polling",
                    "Cloud-only check that never touches the car. Shorter catches trips sooner but costs more."),
    "DRIVING_EVERY": ("num", 1, 15, 60, "While driving, read every (min)", "Polling", ""),
    "CHARGING_EVERY": ("num", 5, 60, 60, "While charging, read every (min)", "Polling", ""),
    "PARKED_REREAD": ("num", 1, 24, 3600, "Parked but awake, re-read every (h)", "Polling", ""),
    "STALE_WAKE_AFTER": ("num", 0, 168, 3600, "Wake & read if no reading for (h, 0 = never)", "Polling",
                         "Each wake costs about 2 cents. Failed wakes are not retried."),
    "MONTHLY_BUDGET": ("num", 0, 9.5, 1, "Monthly spend cap ($)", "Cost",
                       "Tesla gives a $10 monthly credit, so anything under $10 is a $0 bill."),
    "LOW_BATTERY_PCT": ("num", 5, 50, 1, "Low battery alert below (%)", "Alerts", ""),
    "LOW_BATTERY_HOUR": ("num", 0, 23, 1, "...checked after this hour (0-23)", "Alerts", ""),
    "TYRE_LOW_BAR": ("num", 2.0, 3.2, 1, "Tyre alert below (bar)", "Alerts", "2.7 bar = 39 psi"),
    "TYRE_SPREAD_BAR": ("num", 0.1, 1.0, 1, "...or this far below the highest tyre (bar)", "Alerts", ""),
    "ALERT_ON": ("flags", None, None, None, "Email alerts", "Alerts", ""),
    "HOME_RATE": ("num", 0, 1, 1, "Home electricity rate for comparison ($/kWh)", "Cost",
                  "e.g. your overnight rate. Supercharging sessions show what the same kWh would cost at home."),
    "HOME_LOSS_PCT": ("num", 0, 30, 1, "Home charging loss (%)", "Cost",
                      "Energy lost between the wall and the battery. 10% means each battery kWh takes 1.11 kWh from the wall."),
    "PACK_KWH": ("num", 50, 110, 1, "Usable battery size (kWh) for efficiency", "Display", ""),
    "UNITS": ("units", None, None, None, "Units", "Display", ""),
}

log = logging.getLogger("tesla")


# ---------------------------------------------------------------- metrics
class Metrics:
    def __init__(self):
        self.lock = threading.Lock()
        self.gauges: dict[tuple[str, tuple], float] = {}
        self.counters: dict[tuple[str, tuple], float] = {}

    def set(self, name, value, **labels):
        if value is None:
            return
        with self.lock:
            self.gauges[(name, tuple(sorted(labels.items())))] = float(value)

    def inc(self, name, n=1, **labels):
        with self.lock:
            k = (name, tuple(sorted(labels.items())))
            self.counters[k] = self.counters.get(k, 0) + n

    def render(self) -> str:
        out = []
        with self.lock:
            for kind, store in (("gauge", self.gauges), ("counter", self.counters)):
                seen = set()
                for (name, labels), v in sorted(store.items()):
                    if name not in seen:
                        out.append(f"# TYPE {name} {kind}")
                        seen.add(name)
                    lab = ",".join(f'{k}="{v2}"' for k, v2 in labels)
                    out.append(f"{name}{{{lab}}} {v}" if lab else f"{name} {v}")
        return "\n".join(out) + "\n"


M = Metrics()


class _Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        webapp.handle(self, "POST")

    def do_GET(self):
        path = self.path.split("?")[0].rstrip("/") or "/"
        if path != "/metrics":
            webapp.handle(self, "GET")
            return
        body, ctype = M.render().encode(), "text/plain; version=0.0.4"
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


# ---------------------------------------------------------------- storage
def db() -> sqlite3.Connection:
    DATA.mkdir(parents=True, exist_ok=True)
    os.chmod(DATA, 0o700)
    c = sqlite3.connect(DB_PATH, timeout=10)
    c.row_factory = sqlite3.Row
    c.executescript("""
    CREATE TABLE IF NOT EXISTS states(ts INTEGER, state TEXT);
    CREATE TABLE IF NOT EXISTS reads(
      ts INTEGER, mode TEXT, battery_level REAL, usable_battery_level REAL, battery_range REAL,
      est_battery_range REAL, charge_limit_soc REAL, charging_state TEXT, charger_power REAL,
      charge_energy_added REAL, odometer REAL, latitude REAL, longitude REAL, heading REAL,
      speed REAL, power REAL, shift_state TEXT, inside_temp REAL, outside_temp REAL,
      is_climate_on INTEGER, locked INTEGER, sentry_mode INTEGER, is_user_present INTEGER,
      tpms_fl REAL, tpms_fr REAL, tpms_rl REAL, tpms_rr REAL, car_version TEXT, raw BLOB);
    CREATE TABLE IF NOT EXISTS alerts(ts INTEGER, kind TEXT, key TEXT, message TEXT, result TEXT);
    CREATE TABLE IF NOT EXISTS usage(day TEXT PRIMARY KEY, requests INTEGER, dollars REAL DEFAULT 0);
    CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT);
    CREATE TABLE IF NOT EXISTS sc_sessions(session_id TEXT PRIMARY KEY, start TEXT, raw TEXT);
    CREATE TABLE IF NOT EXISTS events(ts INTEGER, kind TEXT, message TEXT);
    CREATE TABLE IF NOT EXISTS web_sessions(token TEXT PRIMARY KEY, created INTEGER, ip TEXT);
    CREATE INDEX IF NOT EXISTS reads_ts ON reads(ts);
    """ + extras.SCHEMA + """
    CREATE INDEX IF NOT EXISTS states_ts ON states(ts);
    """)
    if "dollars" not in [r[1] for r in c.execute("PRAGMA table_info(usage)")]:
        c.execute("ALTER TABLE usage ADD COLUMN dollars REAL DEFAULT 0")
        c.execute("UPDATE usage SET dollars = requests * 0.002")
        c.commit()
    return c


READ_COLS = ["battery_level", "usable_battery_level", "battery_range", "est_battery_range",
             "charge_limit_soc", "charging_state", "charger_power", "charge_energy_added",
             "odometer", "latitude", "longitude", "heading", "speed", "power", "shift_state",
             "inside_temp", "outside_temp", "is_climate_on", "locked", "sentry_mode",
             "is_user_present", "tpms_fl", "tpms_fr", "tpms_rl", "tpms_rr", "car_version"]


def flatten(r: dict) -> dict:
    c, cl, d, v = (r.get(k) or {} for k in ("charge_state", "climate_state", "drive_state", "vehicle_state"))
    loc = r.get("location_data") or {}
    pick = lambda *srcs_key: next((s[k] for s, k in srcs_key if s.get(k) is not None), None)  # noqa: E731
    return {
        "battery_level": c.get("battery_level"), "usable_battery_level": c.get("usable_battery_level"),
        "battery_range": c.get("battery_range"), "est_battery_range": c.get("est_battery_range"),
        "charge_limit_soc": c.get("charge_limit_soc"), "charging_state": c.get("charging_state"),
        "charger_power": c.get("charger_power"), "charge_energy_added": c.get("charge_energy_added"),
        "odometer": v.get("odometer"),
        "latitude": pick((d, "latitude"), (loc, "latitude")),
        "longitude": pick((d, "longitude"), (loc, "longitude")),
        "heading": pick((d, "heading"), (loc, "heading")),
        "speed": d.get("speed"), "power": d.get("power"), "shift_state": d.get("shift_state"),
        "inside_temp": cl.get("inside_temp"), "outside_temp": cl.get("outside_temp"),
        "is_climate_on": cl.get("is_climate_on"), "locked": v.get("locked"),
        "sentry_mode": v.get("sentry_mode"), "is_user_present": v.get("is_user_present"),
        "tpms_fl": v.get("tpms_pressure_fl"), "tpms_fr": v.get("tpms_pressure_fr"),
        "tpms_rl": v.get("tpms_pressure_rl"), "tpms_rr": v.get("tpms_pressure_rr"),
        "car_version": v.get("car_version"),
    }


def settings_values() -> dict:
    return {k: globals()[k] for k in SETTINGS}


def apply_settings(values: dict) -> dict:
    """Validate and apply; returns the cleaned values. Raises ValueError on bad input."""
    clean = {}
    for k, v in values.items():
        if k not in SETTINGS:
            raise ValueError(f"unknown setting {k}")
        kind, lo, hi = SETTINGS[k][:3]
        if kind == "num":
            v = float(v)
            scale = SETTINGS[k][3]          # values arrive in stored units (seconds, $, bar...)
            if not lo * scale <= v <= hi * scale:
                raise ValueError(f"{SETTINGS[k][4]}: must be {lo}-{hi}")
            if k == "STALE_WAKE_AFTER" and 0 < v < 12 * 3600:
                raise ValueError("wake rule must be 0 (never) or at least 12 h")
            clean[k] = v
        elif kind == "flags":
            clean[k] = {a: bool(v.get(a, ALERT_ON.get(a))) for a in ALERT_ON}
        elif kind == "units":
            ok = {"distance": ("mi", "km"), "pressure": ("psi", "bar"), "temp": ("F", "C")}
            clean[k] = {a: (v.get(a) if v.get(a) in ok[a] else UNITS[a]) for a in ok}
    for k, v in clean.items():
        globals()[k] = v
    M.set("tesla_monthly_budget_dollars", MONTHLY_BUDGET)
    return clean


def load_settings(c: sqlite3.Connection):
    saved = {r[0]: json.loads(r[1]) for r in c.execute("SELECT key, value FROM settings")}
    try:
        apply_settings({k: v for k, v in saved.items() if k in SETTINGS})
    except (ValueError, TypeError, AttributeError) as e:
        log.warning("ignoring saved settings: %s", e)


def save_settings(c: sqlite3.Connection, values: dict) -> dict:
    clean = apply_settings(values)
    for k, v in clean.items():
        c.execute("INSERT INTO settings VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                  (k, json.dumps(v)))
    c.commit()
    return clean


def add_event(c: sqlite3.Connection, kind: str, message: str):
    c.execute("INSERT INTO events VALUES(?,?,?)", (int(time.time()), kind, message))
    c.commit()


# ---------------------------------------------------------------- Tesla API
class Tesla:
    def __init__(self, store: sqlite3.Connection):
        self.db = store
        self.client_id = (CFG / "client_id").read_text().strip()

    # tokens --------------------------------------------------------
    def _tokens(self) -> dict:
        return json.loads((CFG / "tokens.json").read_text())

    def _save_tokens(self, tok: dict):
        tok["obtained_at"] = int(time.time())
        tmp = CFG / "tokens.json.tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(tok, f)
        os.replace(tmp, CFG / "tokens.json")

    def refresh_if_needed(self, force=False):
        tok = self._tokens()
        expires = tok.get("obtained_at", 0) + tok.get("expires_in", 0)
        if not force and time.time() < expires - 1800:
            return
        form = urllib.parse.urlencode({"grant_type": "refresh_token", "client_id": self.client_id,
                                       "refresh_token": tok["refresh_token"]}).encode()
        try:
            with urllib.request.urlopen(urllib.request.Request(TOKEN_URL, data=form), timeout=30) as r:
                new = json.loads(r.read())
        except urllib.error.HTTPError as e:
            M.inc("tesla_token_refresh_total", result="fail")
            raise RuntimeError(f"token refresh HTTP {e.code}: {e.read().decode(errors='replace')[:200]}")
        new.setdefault("refresh_token", tok["refresh_token"])
        self._save_tokens(new)
        M.inc("tesla_token_refresh_total", result="ok")
        log.info("token refreshed (expires_in=%s)", new.get("expires_in"))

    # budget --------------------------------------------------------
    def used_today(self) -> int:
        row = self.db.execute("SELECT requests FROM usage WHERE day=?", (today(),)).fetchone()
        return row[0] if row else 0

    def spent(self, month_only_before_today=False) -> float:
        d = dt.date.today()
        sql = "SELECT COALESCE(SUM(dollars),0) FROM usage WHERE day >= ? AND day " + \
              ("< ?" if month_only_before_today else "<= ?")
        return self.db.execute(sql, (d.replace(day=1).isoformat(), d.isoformat())).fetchone()[0]

    def spent_today(self) -> float:
        row = self.db.execute("SELECT dollars FROM usage WHERE day=?", (today(),)).fetchone()
        return row[0] or 0 if row else 0

    def daily_allowance(self) -> float:
        d = dt.date.today()
        nxt = (d.replace(day=28) + dt.timedelta(days=4)).replace(day=1)
        days_left = (nxt - d).days                       # including today
        return max(0.0, (MONTHLY_BUDGET - self.spent(month_only_before_today=True)) / days_left)

    def can_afford(self, endpoint: str) -> bool:
        price = PRICE.get(endpoint, 1 / 500)
        return (self.spent() + price <= MONTHLY_BUDGET
                and self.spent_today() + price <= self.daily_allowance())

    def _count(self, endpoint: str):
        self.db.execute("INSERT INTO usage(day, requests, dollars) VALUES(?,1,?) ON CONFLICT(day) DO UPDATE "
                        "SET requests=requests+1, dollars=dollars+excluded.dollars",
                        (today(), PRICE.get(endpoint, 1 / 500)))
        self.db.commit()
        self.export_spend()

    def export_spend(self):
        M.set("tesla_requests_today", self.used_today())
        M.set("tesla_spend_today_dollars", round(self.spent_today(), 4))
        M.set("tesla_spend_month_dollars", round(self.spent(), 4))
        M.set("tesla_daily_allowance_dollars", round(self.daily_allowance(), 4))

    # calls ---------------------------------------------------------
    def get(self, path: str, endpoint: str, _retry=True, method="GET") -> tuple[int, dict | None]:
        return self.request(f"/api/1/vehicles/{VIN}{path}", endpoint, _retry, method)

    def request(self, full_path: str, endpoint: str, _retry=True, method="GET") -> tuple[int, dict | None]:
        if not self.can_afford(endpoint):
            return -1, None
        self.refresh_if_needed()
        req = urllib.request.Request(f"{API}{full_path}", method=method,
                                     data=b"" if method == "POST" else None,
                                     headers={"Authorization": f"Bearer {self._tokens()['access_token']}"})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                code, body = r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            code, body = e.code, None
        if code < 500:
            self._count(endpoint)        # Tesla bills every response below 500
        M.inc("tesla_api_requests_total", endpoint=endpoint, code=str(code))
        if code == 401 and _retry:
            self.refresh_if_needed(force=True)
            return self.request(full_path, endpoint, _retry=False, method=method)
        return code, body

    def wake(self) -> int:
        """The only call that wakes the car: the stale-data rule, or the page's Wake & read button."""
        code, _ = self.get("/wake_up", "wake_up", method="POST")
        return code

    def state(self) -> str | None:
        code, body = self.get("", "vehicle")
        return body["response"]["state"] if code == 200 and body else None

    def vehicle_data(self) -> tuple[int, dict | None]:
        eps = urllib.parse.quote("charge_state;climate_state;drive_state;vehicle_state;location_data;"
                                 "vehicle_config;gui_settings")
        code, body = self.get(f"/vehicle_data?endpoints={eps}", "vehicle_data")
        return code, (body or {}).get("response")


def today() -> str:
    return dt.date.today().isoformat()


# ---------------------------------------------------------------- email alerts
class Mailer:
    def __init__(self, store):
        self.db = store

    def _cfg(self) -> dict:
        """SMTP settings: the [email] table of config.toml, or (config_toml = "...") another app's
        TOML file with email_address / email_app_password / email_recipient / smtp_host / smtp_port."""
        e = dict(CONF.get("email") or {})
        if e.get("config_toml"):
            try:
                raw = tomllib.loads(pathlib.Path(e["config_toml"]).expanduser().read_text())
            except (OSError, tomllib.TOMLDecodeError):
                return {}
            flat = {}
            for k, v in raw.items():
                flat.update(v) if isinstance(v, dict) else flat.__setitem__(k, v)
            return flat
        return {"email_address": e.get("address"), "email_app_password": e.get("app_password"),
                "email_recipient": e.get("recipient"), "smtp_host": e.get("smtp_host", "smtp.gmail.com"),
                "smtp_port": e.get("smtp_port", 465)}

    def recent(self, kind: str, key: str, within: float) -> bool:
        row = self.db.execute("SELECT 1 FROM alerts WHERE kind=? AND key=? AND ts>?",
                              (kind, key, int(time.time() - within))).fetchone()
        return row is not None

    def send(self, kind: str, key: str, subject: str, body: str):
        c = self._cfg()
        result = "sent"
        if not ALERT_ON.get(kind, True):
            result = "disabled"
        elif not (c.get("email_address") and c.get("email_app_password") and c.get("email_recipient")):
            result = "not configured"
        else:
            link = f"\n{CONF['public_url']}" if CONF.get("public_url") else ""
            msg = MIMEText(body + f"\n\n-- tesla-tools{link}\n", "plain", "utf-8")
            msg["From"], msg["To"] = c["email_address"], c["email_recipient"]
            msg["Subject"] = f"[Tesla] {subject}"
            try:
                with smtplib.SMTP_SSL(c.get("smtp_host", "smtp.gmail.com"), int(c.get("smtp_port", 465)),
                                      timeout=20) as s:
                    s.login(c["email_address"], c["email_app_password"])
                    s.sendmail(c["email_address"], [r.strip() for r in c["email_recipient"].split(",")],
                               msg.as_string())
            except Exception as e:  # noqa: BLE001 - never crash the poller over email
                result = f"error: {e}"[:200]
        self.db.execute("INSERT INTO alerts VALUES(?,?,?,?,?)", (int(time.time()), kind, key, subject, result))
        self.db.commit()
        M.inc("tesla_alert_emails_total", kind=kind, result="sent" if result == "sent" else
              "disabled" if result == "disabled" else "failed")
        log.info("alert %s/%s: %s -> %s", kind, key, subject, result)


def bar_psi(b):
    return f"{b:.2f} bar / {b * 14.5038:.0f} psi"


# ---------------------------------------------------------------- the poller
class Poller:
    def __init__(self):
        self.db = db()
        self.api = Tesla(self.db)
        self.mail = Mailer(self.db)
        self.api.export_spend()
        self.streak = 0
        self.mode = "unknown"          # asleep | online | parked | driving | charging | unknown
        self.last_read_ts = 0.0
        self.read_this_wake = False
        self.last = None               # last flattened read
        self.unlocked_alerted = False
        self.waking_since = 0.0
        self.next_wake_ok = 0.0
        self.actions: queue.Queue = queue.Queue()   # from the web app: "read" | "wake_read"
        self.kick = threading.Event()
        self.next_poll_at = 0.0
        self.manual_wake = False
        row =self.db.execute("SELECT * FROM reads ORDER BY ts DESC LIMIT 1").fetchone()
        if row:
            self.last = dict(row)
            self.last_read_ts = row["ts"]
            self.export(self.last, row["ts"])

    # metrics ----------------------------------------------------------
    def export(self, f: dict, ts: float):
        for col in ("battery_level", "usable_battery_level", "charge_limit_soc", "charger_power",
                    "charge_energy_added", "odometer", "inside_temp", "outside_temp", "speed", "power"):
            M.set(f"tesla_{col}", f.get(col))
        M.set("tesla_range_miles", f.get("battery_range"))
        M.set("tesla_est_range_miles", f.get("est_battery_range"))
        M.set("tesla_charging", 1 if f.get("charging_state") in ("Charging", "Starting") else 0)
        M.set("tesla_plugged_in", 0 if f.get("charging_state") in (None, "Disconnected") else 1)
        for b in ("locked", "sentry_mode", "is_climate_on", "is_user_present"):
            if f.get(b) is not None:
                M.set(f"tesla_{b}", int(bool(f[b])))
        for w in ("fl", "fr", "rl", "rr"):
            M.set("tesla_tpms_bar", f.get(f"tpms_{w}"), wheel=w)
        M.set("tesla_last_read_timestamp_seconds", ts)

    def event(self, kind: str, message: str):
        add_event(self.db, kind, message)

    def set_mode(self, mode: str):
        if mode != self.mode:
            log.info("mode %s -> %s", self.mode, mode)
            if self.mode != "unknown":          # "unknown" = just started: not a real change
                self.event("mode", f"{self.mode} → {mode}")
        self.mode = mode
        for m in ("asleep", "online", "parked", "driving", "charging", "unknown"):
            M.set("tesla_mode", 1 if m == mode else 0, mode=m)

    # one read of the car ----------------------------------------------
    def read_car(self):
        code, r = self.api.vehicle_data()
        if code == -1:                    # out of budget: stop reading, fall back to state checks
            M.set("tesla_budget_paused", 1)
            self.set_mode("online")
            return
        if code == 408:                   # went to sleep / unreachable: never wake it
            self.streak = 0
            self.set_mode("asleep")
            return
        if code != 200 or not r:
            log.warning("vehicle_data HTTP %s", code)
            M.inc("tesla_poller_errors_total", where="vehicle_data")
            return
        now = time.time()
        f = flatten(r)
        moving = f["shift_state"] in ("D", "R", "N") or (f["speed"] or 0) > 0
        charging = f["charging_state"] in ("Charging", "Starting")
        mode = "driving" if moving else "charging" if charging else "parked"
        self.db.execute(f"INSERT INTO reads(ts, mode, {', '.join(READ_COLS)}, raw) VALUES "
                        f"({', '.join('?' * (len(READ_COLS) + 3))})",
                        [int(now), mode, *[f[c] for c in READ_COLS], gzip.compress(json.dumps(r).encode())])
        self.db.commit()
        self.last, self.last_read_ts, self.read_this_wake = f, now, True
        self.export(f, now)
        drive_ended = self.mode == "driving" and mode != "driving"
        self.set_mode(mode)
        if drive_ended:
            self.sync_alerts()
        r2 = self.db.execute("SELECT value FROM settings WHERE key='nearby'").fetchone()
        if not r2 or now - json.loads(r2[0]).get("ts", 0) >= NEARBY_EVERY:
            try:
                extras.sync_nearby(self.api, self.db, VIN)     # the car is awake right now anyway
            except Exception:  # noqa: BLE001
                log.exception("nearby sync failed")
        self.check_car_alerts(f)

    # alerts that need a fresh read --------------------------------------
    def check_car_alerts(self, f: dict):
        if self.mode == "parked" and f["locked"] is False and not f["is_user_present"]:
            if not self.unlocked_alerted:
                self.unlocked_alerted = True
                self.mail.send("unlocked", "car", "Car is parked and UNLOCKED",
                               f"The car reported unlocked with nobody in it at "
                               f"{time.strftime('%H:%M')}.\n{maps(f)}")
        elif f["locked"]:
            self.unlocked_alerted = False
        tyres = {w: f[f"tpms_{w}"] for w in ("fl", "fr", "rl", "rr") if f[f"tpms_{w}"]}
        if tyres:
            top = max(tyres.values())
            for w, p in tyres.items():
                if (p < TYRE_LOW_BAR or top - p > TYRE_SPREAD_BAR) and not self.mail.recent("tyre", w, 86400):
                    self.mail.send("tyre", w, f"Low tyre pressure: {w.upper()} {bar_psi(p)}",
                                   "All tyres: " + ", ".join(f"{k.upper()} {bar_psi(v)}" for k, v in tyres.items()))

    # alerts from what we already know (no car call) ----------------------
    def check_evening_battery(self):
        f = self.last
        if not f or dt.datetime.now().hour < LOW_BATTERY_HOUR:
            return
        if (f["battery_level"] or 100) < LOW_BATTERY_PCT and f["charging_state"] in (None, "Disconnected"):
            if not self.mail.recent("battery", today(), 20 * 3600):
                age = time.strftime("%a %H:%M", time.localtime(self.last_read_ts))
                self.mail.send("battery", today(), f"Battery {f['battery_level']:.0f}% and not plugged in",
                               f"Last reading {age}: {f['battery_level']:.0f}%, "
                               f"{f['battery_range'] or 0:.0f} mi range, not plugged in.")

    # main loop ----------------------------------------------------------
    def maybe_sync_supercharging(self, force=False):
        """Tesla's Supercharger history: once a day (one request, ~$0.06/month)."""
        r = self.db.execute("SELECT value FROM settings WHERE key='sc_last_sync'").fetchone()
        last = json.loads(r[0]) if r else 0
        if not force and time.time() - last < 86400:
            return
        try:
            n = supercharger.sync(self.api, self.db, VIN)
            if n or force:
                self.event("supercharging", f"Supercharger history synced: {n} new session(s)")
        except Exception:  # noqa: BLE001 - never let this stop polling
            log.exception("supercharger sync failed")

    def sync_alerts(self):
        """recent_alerts keeps only the last dozen: store them, email faults / 'service required'."""
        first = self.db.execute("SELECT COUNT(*) FROM car_alerts").fetchone()[0] == 0
        try:
            new = extras.sync_alerts(self.api, self.db, VIN)
        except Exception:  # noqa: BLE001
            log.exception("alert sync failed")
            return
        if first:                       # seeding: alerts from before we started are history, not news
            if new:
                self.event("alert", f"Car alert history: {len(new)} recent alert(s) saved")
            return
        for a in new:
            self.event("alert", f"Car alert: {a['user_text'] or a['name']} ({a['name']})")
            if extras.needs_service(a["name"], a["user_text"]) and time.time() - a["ts"] < 7 * 86400:
                when = dt.datetime.fromtimestamp(a["ts"]).strftime("%a %d %b %H:%M")
                self.mail.send("car_alert", f"{a['name']}:{a['ts']}", f"Car alert: {a['user_text'] or a['name']}",
                               f"The car reported at {when}:\n\n  {a['user_text'] or '(no message)'}\n  code {a['name']}\n\n"
                               "If the car shows 'Service is required', book a service visit in the Tesla app.")

    def maybe_sync_extras(self, force=False):
        """Alerts, service visits, software, drivers: once a day (about 5 requests)."""
        r = self.db.execute("SELECT value FROM settings WHERE key='extras_last_sync'").fetchone()
        if not force and r and time.time() - json.loads(r[0]) < 86400:
            return
        self.sync_alerts()
        try:
            out = extras.sync_daily(self.api, self.db, VIN)
            if out["visits_new"] or out["new_version"] or force:
                self.event("service", f"Service/software synced: {out['visits_new']} new visit(s)"
                           + (f", software updated to {out['new_version']}" if out["new_version"] else ""))
        except Exception:  # noqa: BLE001
            log.exception("extras sync failed")

    def step(self) -> float:
        """Do one action, return seconds until the next one."""
        self.maybe_sync_supercharging()
        self.maybe_sync_extras()
        if self.mode in ("driving", "charging"):
            self.read_car()
            if self.mode == "driving":
                return DRIVING_EVERY
            if self.mode == "charging":
                return CHARGING_EVERY
            return STATE_EVERY

        st = self.api.state()
        if st is None:
            if not self.api.can_afford("vehicle"):
                month = dt.date.today().strftime("%Y-%m")
                if self.api.spent() + PRICE["vehicle"] > MONTHLY_BUDGET and not self.mail.recent("budget", month, 40 * 86400):
                    self.mail.send("budget", month, f"Monthly budget ${MONTHLY_BUDGET:.2f} reached",
                                   f"Estimated Tesla API spend this month is ${self.api.spent():.2f}. "
                                   "Polling is paused until the 1st so the bill stays at $0.")
                M.set("tesla_budget_paused", 1)
                return STATE_EVERY
            M.inc("tesla_poller_errors_total", where="state")
            return STATE_EVERY
        M.set("tesla_budget_paused", 0)
        self.db.execute("INSERT INTO states VALUES(?,?)", (int(time.time()), st))
        self.db.commit()
        M.set("tesla_online", 1 if st == "online" else 0)
        M.set("tesla_last_state_check_timestamp_seconds", time.time())

        stale = bool(STALE_WAKE_AFTER) and time.time() - self.last_read_ts >= STALE_WAKE_AFTER
        if self.waking_since:                       # we woke it: read as soon as it is online
            if st == "online":
                self.waking_since = 0
                self.streak, self.read_this_wake = 1, False
                self.set_mode("online")
                log.info("woken car is online, reading once")
                self.event("wake", "woken car came online; read it once")
                self.read_car()
                return DRIVING_EVERY if self.mode == "driving" else \
                    CHARGING_EVERY if self.mode == "charging" else STATE_EVERY
            if time.time() - self.waking_since < WAKE_WAIT:
                return 120
            log.warning("car did not come online within %ss of wake_up", WAKE_WAIT)
            M.inc("tesla_wakes_total", result="timeout")
            self.event("wake", f"car did not come online within {WAKE_WAIT // 60} min of the wake; not retried")
            self.waking_since = 0
            self.next_wake_ok = time.time() + STALE_WAKE_AFTER   # no retries: next slot is 24 h on

        if st != "online":
            self.streak, self.read_this_wake = 0, False
            self.set_mode("asleep" if st == "asleep" else "unknown")
            if STALE_WAKE_AFTER and stale and time.time() >= self.next_wake_ok:
                code = self.api.wake()
                self.event("wake", f"no reading for {(time.time() - self.last_read_ts) / 3600:.0f} h: "
                                   f"sent wake_up (HTTP {code})")
                log.info("no car read for %.1f h: wake_up -> HTTP %s",
                         (time.time() - self.last_read_ts) / 3600, code)
                if code == 200:
                    M.inc("tesla_wakes_total", result="sent")
                    self.waking_since = time.time()
                    return 120
                M.inc("tesla_wakes_total", result="error")
                self.next_wake_ok = time.time() + STALE_WAKE_AFTER
        elif stale and not self.read_this_wake:     # online, data stale: no need to wait for a streak
            self.streak += 1
            self.read_car()
            return DRIVING_EVERY if self.mode == "driving" else \
                CHARGING_EVERY if self.mode == "charging" else STATE_EVERY
        else:
            self.streak += 1
            if self.mode in ("asleep", "unknown"):
                self.set_mode("online")
            staying_awake = self.streak >= ONLINE_STREAK_NEEDED
            due = not self.read_this_wake or time.time() - self.last_read_ts >= PARKED_REREAD
            if staying_awake and due:
                self.read_car()
                if self.mode == "driving":
                    return DRIVING_EVERY
                if self.mode == "charging":
                    return CHARGING_EVERY
        self.check_evening_battery()
        return STATE_EVERY

    def do_action(self, action: str) -> float | None:
        """A button on the web page. Returns the next wait, or None to keep the current schedule."""
        if action == "read":
            st = self.api.state()
            if st == "online":
                self.event("manual", "Read now: car is awake, reading it")
                self.read_car()
                return DRIVING_EVERY if self.mode == "driving" else \
                    CHARGING_EVERY if self.mode == "charging" else STATE_EVERY
            self.event("manual", f"Read now: car is {st or 'unreachable'}, left alone (not woken)")
            return None
        if action == "extras_sync":
            self.maybe_sync_extras(force=True)
            return None
        if action == "sc_sync":
            self.maybe_sync_supercharging(force=True)
            return None
        if action == "wake_read":
            code = self.api.wake()
            self.event("manual", f"Wake & read: sent wake_up (HTTP {code})")
            if code == 200:
                M.inc("tesla_wakes_total", result="sent")
                self.waking_since = time.time()
                return 60
            return None
        return None

    def run(self):
        self.set_mode("unknown")
        failing_since = None
        while True:
            try:
                wait = self.step()
                failing_since = None
            except Exception as e:  # noqa: BLE001 - keep running; email if it stays broken
                log.exception("poll failed")
                M.inc("tesla_poller_errors_total", where="loop")
                failing_since = failing_since or time.time()
                if time.time() - failing_since > 3600 and not self.mail.recent("broken", "loop", 86400):
                    self.mail.send("broken", "loop", "Tesla poller keeps failing",
                                   f"Failing for over an hour. Last error: {e}\n"
                                   "If it is a token error, run ~/tesla-tools/login.py again.")
                wait = STATE_EVERY
            M.set("tesla_next_poll_seconds", wait)
            self.next_poll_at = time.time() + wait
            # Sleep until the next poll, but wake early for a web-page button.
            while self.kick.wait(max(0.0, self.next_poll_at - time.time())):
                self.kick.clear()
                while not self.actions.empty():
                    try:
                        w = self.do_action(self.actions.get_nowait())
                    except Exception:  # noqa: BLE001
                        log.exception("web action failed")
                        w = None
                    if w is not None:
                        self.next_poll_at = time.time() + w
                        M.set("tesla_next_poll_seconds", w)


def resolve_vin():
    """config vin, else the one cached in the settings table, else the first car on the account."""
    global VIN
    if VIN:
        return
    c = db()
    r = c.execute("SELECT value FROM settings WHERE key='vin'").fetchone()
    if r:
        VIN = json.loads(r[0])
        return
    code, body = Tesla(c).request("/api/1/vehicles", "vehicle")
    cars = (body or {}).get("response") or []
    if code != 200 or not cars:
        raise SystemExit(f"could not find a car on this account (HTTP {code}); set vin in config.toml")
    VIN = cars[0]["vin"]
    c.execute("INSERT INTO settings VALUES('vin', ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (json.dumps(VIN),))
    c.commit()
    log.info("using %s (%s); set vin in config.toml to choose another", cars[0].get("display_name"), VIN)


def maps(f):
    if f.get("latitude") is None:
        return ""
    return f"https://maps.google.com/?q={f['latitude']},{f['longitude']}"


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    load_settings(db())
    M.set("tesla_monthly_budget_dollars", MONTHLY_BUDGET)
    resolve_vin()
    poller = Poller()
    webapp.setup(sys.modules[__name__], poller)
    srv = ThreadingHTTPServer(METRICS_ADDR, _Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    poller.run()
