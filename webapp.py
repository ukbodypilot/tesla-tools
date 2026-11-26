"""The Tesla web app: static single page + JSON API, served by the poller's HTTP server.

Reached directly on the LAN (http://<host>:9877/) or through the radio gateway at /tesla/
(which passes the real client address in X-Forwarded-For). Access rule:
  * LAN / Tailscale / localhost clients: open, like the other LAN pages.
  * Anyone else (i.e. over the gateway's Cloudflare tunnel): needs this app's own password,
    set on the Settings tab from the LAN. With no password set, outside access is refused.
POSTs need the X-Requested-With: tesla header, which a cross-site form cannot send.
All URLs in the page are relative, so it works under any prefix.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import ipaddress
import json
import logging
import mimetypes
import pathlib
import secrets
import sqlite3
import threading
import time
import urllib.parse
from http import cookies

import analysis
import charging
import extras
import supercharger

log = logging.getLogger("tesla.web")
WEB = pathlib.Path(__file__).resolve().parent / "web"
COOKIE = "tesla_s"
SESSION_DAYS = 30
svc = None        # the tesla_service module (globals = live settings)
poller = None
_fails: dict[str, list[float]] = {}
_fails_lock = threading.Lock()


def setup(service_module, poller_obj):
    global svc, poller
    svc, poller = service_module, poller_obj


def _db() -> sqlite3.Connection:
    c = sqlite3.connect(svc.DB_PATH, timeout=10)
    c.row_factory = sqlite3.Row
    return c


# ------------------------------------------------------------------ access
def client_ip(h) -> str:
    peer = h.client_address[0] if h.client_address else ""
    if peer in ("127.0.0.1", "::1") and h.headers.get("X-Forwarded-For"):
        return h.headers["X-Forwarded-For"].split(",")[0].strip()   # only trusted from the local proxy
    return peer


# Explicit list: ipaddress.is_private also counts documentation/benchmark ranges as private.
LOCAL_NETS = [ipaddress.ip_network(n) for n in (
    "127.0.0.0/8", "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "100.64.0.0/10",   # LAN + Tailscale
    "::1/128", "fc00::/7", "fe80::/10")]


def is_local(ip: str) -> bool:
    try:
        a = ipaddress.ip_address(ip)
    except ValueError:
        return False
    if a.version == 6 and a.ipv4_mapped:
        a = a.ipv4_mapped
    return any(a in n for n in LOCAL_NETS)


def _get_setting(c, key):
    r = c.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return json.loads(r[0]) if r else None


def _hash(pw: str, salt: bytes | None = None, it: int = 240_000) -> str:
    salt = salt or secrets.token_bytes(16)
    return f"pbkdf2_sha256${it}${salt.hex()}${hashlib.pbkdf2_hmac('sha256', pw.encode(), salt, it).hex()}"


def _check(pw: str, stored: str) -> bool:
    try:
        _, it, salt, _h = stored.split("$")
        return hmac.compare_digest(_hash(pw, bytes.fromhex(salt), int(it)), stored)
    except (ValueError, AttributeError):
        return False


def _session_ok(h, c) -> bool:
    jar = cookies.SimpleCookie(h.headers.get("Cookie", ""))
    tok = jar[COOKIE].value if COOKIE in jar else None
    if not tok:
        return False
    r = c.execute("SELECT created FROM web_sessions WHERE token=?", (hashlib.sha256(tok.encode()).hexdigest(),)).fetchone()
    return bool(r and time.time() - r[0] < SESSION_DAYS * 86400)


def access(h, c) -> str:
    """'local' | 'session' | 'login' (needs password) | 'off' (no password set yet)."""
    if is_local(client_ip(h)):
        return "local"
    if not _get_setting(c, "web_password_hash"):
        return "off"
    return "session" if _session_ok(h, c) else "login"


def _throttled(ip: str) -> bool:
    now = time.time()
    with _fails_lock:
        _fails[ip] = [t for t in _fails.get(ip, []) if now - t < 600]
        return len(_fails[ip]) >= 5


def _fail(ip: str):
    with _fails_lock:
        _fails.setdefault(ip, []).append(time.time())
    log.warning("[WebAccess] failed Tesla login from %s", ip)


# ------------------------------------------------------------------ responses
def _send(h, code: int, body: bytes, ctype: str, extra: dict | None = None):
    h.send_response(code)
    h.send_header("Content-Type", ctype)
    h.send_header("Content-Length", str(len(body)))
    h.send_header("Cache-Control", "no-store" if ctype.startswith(("application/json", "text/html")) else "max-age=3600")
    h.send_header("X-Content-Type-Options", "nosniff")
    h.send_header("Referrer-Policy", "strict-origin-when-cross-origin")   # OSM tiles 403 without a Referer
    for k, v in (extra or {}).items():
        h.send_header(k, v)
    h.end_headers()
    if h.command != "HEAD":
        h.wfile.write(body)


def _json(h, obj, code=200, extra=None):
    _send(h, code, json.dumps(obj, default=str).encode(), "application/json", extra)


def _static(h, rel: str):
    p = (WEB / rel).resolve()
    if WEB not in p.parents or not p.is_file():
        _send(h, 404, b"not found", "text/plain")
        return
    ctype = mimetypes.guess_type(p.name)[0] or "application/octet-stream"
    if p.suffix == ".woff2":
        ctype = "font/woff2"
    _send(h, 200, p.read_bytes(), ctype + ("; charset=utf-8" if ctype.startswith(("text/", "application/javascript")) else ""))


# ------------------------------------------------------------------ data
def _raw_latest(c) -> dict:
    import gzip
    r = c.execute("SELECT raw FROM reads WHERE raw IS NOT NULL ORDER BY ts DESC LIMIT 1").fetchone()
    return json.loads(gzip.decompress(r[0])) if r else {}


def status(c) -> dict:
    raw = _raw_latest(c)
    cs, cl, vs = raw.get("charge_state", {}), raw.get("climate_state", {}), raw.get("vehicle_state", {})
    vc, gs = raw.get("vehicle_config", {}), raw.get("gui_settings", {})
    last = c.execute("SELECT * FROM reads ORDER BY ts DESC LIMIT 1").fetchone()
    st = c.execute("SELECT ts, state FROM states ORDER BY ts DESC LIMIT 1").fetchone()
    car = {k: last[k] for k in last.keys() if k != "raw"} if last else {}
    # a car that has gone to sleep since its last reading cannot be in Sentry Mode (Sentry keeps it awake), and the
    # reading's climate is over too: say what is true now instead of showing the old reading as current
    slept = bool(st and last and st["state"] == "asleep" and st["ts"] > last["ts"])
    api = object.__new__(svc.Tesla)   # budget helpers only need .db; use this request's connection
    api.db = c
    return {
        "now": time.time(),
        "name": svc.CONF["name"] or raw.get("display_name") or "Tesla",
        "vin": svc.VIN,
        "mode": poller.mode,
        "state": st["state"] if st else None, "state_ts": st["ts"] if st else None,
        "last_read_ts": poller.last_read_ts or None,
        "next_poll_at": poller.next_poll_at or None,
        "waking": bool(poller.waking_since),
        "car": car,
        "place": charging._place(car.get("latitude"), car.get("longitude"), charging._places()) if car else None,
        "details": {
            "charge_limit": cs.get("charge_limit_soc"), "charge_amps": cs.get("charge_current_request"),
            "charge_amps_max": cs.get("charge_current_request_max"),
            "scheduled_charging": cs.get("scheduled_charging_mode"),
            "scheduled_start": cs.get("scheduled_charging_start_time"),
            "departure": cs.get("scheduled_departure_time_minutes") or cs.get("scheduled_departure_time"),
            "off_peak": cs.get("off_peak_charging_enabled"), "preconditioning": cs.get("preconditioning_enabled"),
            "port_open": cs.get("charge_port_door_open"), "port_latch": cs.get("charge_port_latch"),
            "minutes_to_full": cs.get("minutes_to_full_charge"), "charge_rate": cs.get("charge_rate"),
            "charger_voltage": cs.get("charger_voltage"), "charger_current": cs.get("charger_actual_current"),
            "driver_temp": cl.get("driver_temp_setting"), "cabin_overheat": cl.get("cabin_overheat_protection"),
            "seat_heater_left": cl.get("seat_heater_left"), "defrost": cl.get("is_front_defroster_on"),
            "sentry": False if slept else vs.get("sentry_mode"), "valet": vs.get("valet_mode"),
            "asleep_since_read": slept,
            "speed_limit": (vs.get("speed_limit_mode") or {}).get("active"),
            "software": vs.get("car_version"),
            "update": (vs.get("software_update") or {}).get("status") or None,
            "update_version": (vs.get("software_update") or {}).get("version") or None,
            "doors": {k: vs.get(k) for k in ("df", "pf", "dr", "pr")},
            "windows": {k: vs.get(k) for k in ("fd_window", "fp_window", "rd_window", "rp_window")},
            "trunks": {"front": vs.get("ft"), "rear": vs.get("rt")},
            "falcon": {"left": vs.get("rd"), "right": vs.get("rp")},
            "car_type": vc.get("car_type"), "trim": vc.get("trim_badging"), "color": vc.get("exterior_color"),
            "wheels": vc.get("wheel_type"), "gui_distance": gs.get("gui_distance_units"),
        },
        "budget": {"month": round(api.spent(), 3), "today": round(api.spent_today(), 3),
                   "allowance_today": round(api.daily_allowance(), 3), "cap": svc.MONTHLY_BUDGET,
                   "requests_today": api.used_today()},
        "units": svc.UNITS,
        "pack_kwh": svc.PACK_KWH,
        "grafana": svc.GRAFANA_URL,
        "home_rate_label": svc.CONF["home_rate_label"],
    }


def settings_payload(c, acc: str) -> dict:
    vals = svc.settings_values()
    out = []
    for k, (kind, lo, hi, scale, label, group, help_) in svc.SETTINGS.items():
        v = vals[k]
        out.append({"key": k, "kind": kind, "min": lo, "max": hi, "label": label, "group": group, "help": help_,
                    "value": round(v / scale, 3) if kind == "num" else v})
    per_month = 30 * 86400 / svc.STATE_EVERY * svc.PRICE["vehicle"]
    return {"settings": out, "alert_kinds": list(svc.ALERT_ON),
            "baseline_cost_month": round(per_month, 2),
            "password_set": bool(_get_setting(c, "web_password_hash")),
            "can_set_password": acc == "local",
            "access": acc}


# ------------------------------------------------------------------ routing
def handle(h, method: str):
    url = urllib.parse.urlsplit(h.path)
    path = url.path.rstrip("/") or "/"
    q = dict(urllib.parse.parse_qsl(url.query))
    c = _db()
    try:
        if path.startswith("/static/"):
            return _static(h, path[len("/static/"):])
        if path == "/charging":                                # old bookmark
            return _send(h, 302, b"", "text/plain", {"Location": "./#charging"})
        acc = access(h, c)
        if path == "/":
            if acc in ("local", "session"):
                return _static(h, "index.html")
            return _static(h, "login.html")
        if not path.startswith("/api/"):
            return _send(h, 404, b"not found", "text/plain")
        if method == "POST" and h.headers.get("X-Requested-With") != "tesla":
            return _json(h, {"error": "bad request"}, 400)
        body = {}
        if method == "POST":
            n = int(h.headers.get("Content-Length") or 0)
            if n > 65536:
                return _json(h, {"error": "too large"}, 413)
            body = json.loads(h.rfile.read(n) or b"{}") if n else {}

        if path == "/api/login" and method == "POST":
            ip = client_ip(h)
            if acc == "off":
                return _json(h, {"error": "Remote access is off until a password is set from home."}, 403)
            if _throttled(ip):
                return _json(h, {"error": "Too many tries. Wait 10 minutes."}, 429)
            if not _check(str(body.get("password", "")), _get_setting(c, "web_password_hash") or ""):
                _fail(ip)
                return _json(h, {"error": "Wrong password."}, 401)
            tok = secrets.token_urlsafe(32)
            c.execute("INSERT INTO web_sessions VALUES(?,?,?)", (hashlib.sha256(tok.encode()).hexdigest(), int(time.time()), ip))
            c.commit()
            ck = f"{COOKIE}={tok}; Max-Age={SESSION_DAYS * 86400}; Path=/; HttpOnly; SameSite=Strict"
            return _json(h, {"ok": True}, extra={"Set-Cookie": ck})
        if acc not in ("local", "session"):
            return _json(h, {"error": "login required", "access": acc}, 401)

        if path == "/api/status":
            return _json(h, status(c))
        if path == "/api/track":
            hours = min(float(q.get("hours", 24)), 24 * 90)
            return _json(h, analysis.track(svc.DB_PATH, time.time() - hours * 3600))
        if path == "/api/trips":
            return _json(h, analysis.trips(svc.DB_PATH, min(float(q.get("days", 30)), 366), svc.PACK_KWH))
        if path == "/api/charging":
            return _json(h, charging.sessions(svc.DB_PATH))
        if path == "/api/service":
            return _json(h, extras.service_view(svc.DB_PATH))
        if path == "/api/nearby":
            return _json(h, extras.nearby_view(svc.DB_PATH))
        if path == "/api/supercharging":
            return _json(h, {**supercharger.summary(svc.DB_PATH, svc.HOME_RATE, svc.HOME_LOSS_PCT),
                             "home_rate_label": svc.CONF["home_rate_label"]})
        if path == "/api/battery":
            return _json(h, analysis.battery(svc.DB_PATH, min(float(q.get("days", 30)), 366)))
        if path == "/api/activity":
            return _json(h, analysis.activity(svc.DB_PATH, min(float(q.get("days", 7)), 90)))
        if path == "/api/settings" and method == "GET":
            return _json(h, settings_payload(c, acc))
        if path == "/api/settings" and method == "POST":
            vals = {}
            for k, v in (body.get("values") or {}).items():
                spec = svc.SETTINGS.get(k)
                if not spec:
                    return _json(h, {"error": f"unknown setting {k}"}, 400)
                vals[k] = float(v) * spec[3] if spec[0] == "num" else v
            try:
                svc.save_settings(c, vals)
            except (ValueError, TypeError) as e:
                return _json(h, {"error": str(e)}, 400)
            svc.add_event(c, "settings", "Settings changed: " + ", ".join(sorted(vals)))
            return _json(h, settings_payload(c, acc))
        if path == "/api/password" and method == "POST":
            if acc != "local":
                return _json(h, {"error": "The password can only be changed from home (LAN)."}, 403)
            pw = str(body.get("password", ""))
            if body.get("clear"):
                c.execute("DELETE FROM settings WHERE key='web_password_hash'")
                c.execute("DELETE FROM web_sessions")
                c.commit()
                svc.add_event(c, "settings", "Remote access password removed: outside access is off")
                return _json(h, settings_payload(c, acc))
            if len(pw) < 10:
                return _json(h, {"error": "Use at least 10 characters."}, 400)
            c.execute("INSERT INTO settings VALUES('web_password_hash', ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                      (json.dumps(_hash(pw)),))
            c.execute("DELETE FROM web_sessions")          # a new password signs everyone out
            c.commit()
            svc.add_event(c, "settings", "Remote access password set")
            return _json(h, settings_payload(c, acc))
        if path == "/api/logout" and method == "POST":
            jar = cookies.SimpleCookie(h.headers.get("Cookie", ""))
            if COOKIE in jar:
                c.execute("DELETE FROM web_sessions WHERE token=?", (hashlib.sha256(jar[COOKIE].value.encode()).hexdigest(),))
                c.commit()
            return _json(h, {"ok": True}, extra={"Set-Cookie": f"{COOKIE}=; Max-Age=0; Path=/"})
        if path == "/api/action" and method == "POST":
            action = body.get("action")
            if action not in ("read", "wake_read", "sc_sync", "extras_sync"):
                return _json(h, {"error": "unknown action"}, 400)
            poller.actions.put(action)
            poller.kick.set()
            return _json(h, {"ok": True, "queued": action})
        if path == "/api/test_email" and method == "POST":
            m = svc.Mailer(c)
            on = svc.ALERT_ON.copy()
            m.send("test", dt.datetime.now().isoformat(timespec="seconds"), "Test email from the Tesla page",
                   "If you can read this, Tesla alert emails work.")
            r = c.execute("SELECT result FROM alerts WHERE kind='test' ORDER BY ts DESC LIMIT 1").fetchone()
            svc.ALERT_ON.update(on)
            return _json(h, {"result": r[0] if r else "?"})
        return _json(h, {"error": "not found"}, 404)
    except (BrokenPipeError, ConnectionResetError):
        pass
    except Exception as e:  # noqa: BLE001
        log.exception("web %s %s failed", method, path)
        try:
            _json(h, {"error": f"{type(e).__name__}: {e}"}, 500)
        except (BrokenPipeError, ConnectionResetError):
            pass
    finally:
        c.close()
