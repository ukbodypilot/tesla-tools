"""Alerts, service, software and nearby chargers: Fleet API data beyond vehicle_data.

Works while the car sleeps (tested): recent_alerts, service_history (+ per-visit detail with work
items and cost), service_data, release_notes, drivers. nearby_charging_sites needs the car awake,
so it is only fetched when a reading has just shown the car awake anyway.

recent_alerts only holds the last dozen or so, so every alert is kept here as it is seen
(daily, and after each drive).
"""
from __future__ import annotations

import datetime as dt
import email.utils
import json
import logging
import sqlite3
import time

log = logging.getLogger("tesla.extras")

SCHEMA = """
CREATE TABLE IF NOT EXISTS car_alerts(name TEXT, ts INTEGER, user_text TEXT, audiences TEXT, first_seen INTEGER,
                                      PRIMARY KEY(name, ts));
CREATE TABLE IF NOT EXISTS service_visits(id TEXT PRIMARY KEY, summary TEXT, detail TEXT, fetched INTEGER);
CREATE TABLE IF NOT EXISTS software(version TEXT PRIMARY KEY, first_seen INTEGER, notes TEXT);
"""


def severity(name: str) -> str:
    """Tesla alert codes look like BMS_f035_SW_Isolation: the letter after the first _ is the kind."""
    try:
        k = name.split("_")[1][0]
    except IndexError:
        return "info"
    return {"f": "fault", "w": "warning", "u": "info", "a": "info"}.get(k, "info")


def needs_service(name: str, text: str | None) -> bool:
    return severity(name) == "fault" or "service" in (text or "").lower()


def _setting(c, key, value=None):
    if value is None:
        r = c.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return json.loads(r[0]) if r else None
    c.execute("INSERT INTO settings VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, json.dumps(value)))
    c.commit()


def _get(api, vin, path):
    code, body = api.request(f"/api/1/vehicles/{vin}/{path}", "extras")   # billed as a data request
    return (body or {}).get("response") if code == 200 else None


def sync_alerts(api, c: sqlite3.Connection, vin: str) -> list[dict]:
    """Store recent alerts; return the ones not seen before."""
    r = _get(api, vin, "recent_alerts")
    if r is None:
        return []
    new = []
    for a in r.get("recent_alerts") or []:
        try:
            ts = int(email.utils.parsedate_to_datetime(a["time"]).timestamp())
        except (KeyError, TypeError, ValueError):
            continue
        cur = c.execute("INSERT OR IGNORE INTO car_alerts VALUES(?,?,?,?,?)",
                        (a.get("name"), ts, a.get("user_text") or "", json.dumps(a.get("audiences")), int(time.time())))
        if cur.rowcount:
            new.append({"name": a.get("name"), "ts": ts, "user_text": a.get("user_text") or ""})
    c.commit()
    _setting(c, "alerts_last_sync", time.time())
    return new


def sync_daily(api, c: sqlite3.Connection, vin: str) -> dict:
    """Service visits (details for new or still-open visits), service status, software, drivers."""
    out = {"visits_new": 0, "details": 0, "new_version": None}
    hist = _get(api, vin, "service_history")
    if hist is not None:
        for v in hist:
            vid = str(v.get("service_visit_id"))
            row = c.execute("SELECT summary, detail FROM service_visits WHERE id=?", (vid,)).fetchone()
            changed = row is None or json.loads(row[0]).get("status") != v.get("status") or row[1] is None
            if row is None:
                out["visits_new"] += 1
            detail = row[1] if row else None
            if changed:
                d = _get(api, vin, f"service_history/{vid}")
                if d is not None:
                    detail = json.dumps(d)
                    out["details"] += 1
            c.execute("INSERT INTO service_visits VALUES(?,?,?,?) ON CONFLICT(id) DO UPDATE SET summary=excluded.summary, "
                      "detail=excluded.detail, fetched=excluded.fetched", (vid, json.dumps(v), detail, int(time.time())))
        c.commit()
    sd = _get(api, vin, "service_data")
    if sd is not None:
        _setting(c, "service_data", {"ts": time.time(), "data": sd})
    rn = _get(api, vin, "release_notes")
    if rn is not None:
        ver = rn.get("deployed_version")
        if ver:
            cur = c.execute("INSERT OR IGNORE INTO software VALUES(?,?,?)",
                            (ver, int(time.time()), json.dumps(rn.get("release_notes") or [])))
            if cur.rowcount and c.execute("SELECT COUNT(*) FROM software").fetchone()[0] > 1:
                out["new_version"] = ver
        _setting(c, "release_staged", rn.get("staged_version"))
        c.commit()
    dr = _get(api, vin, "drivers")
    if dr is not None:   # names and access level only; ids and keys are not kept
        _setting(c, "drivers", [{"first": d.get("driver_first_name"), "last": d.get("driver_last_name"),
                                 "access": d.get("granular_access")} for d in dr])
    _setting(c, "extras_last_sync", time.time())
    return out


def sync_nearby(api, c: sqlite3.Connection, vin: str):
    _setting(c, "nearby_tried", time.time())   # a failed try waits NEARBY_EVERY too, never once per read
    r = _get(api, vin, "nearby_charging_sites")
    if r is not None:
        _setting(c, "nearby", {"ts": time.time(), "data": r})


# ------------------------------------------------------------------ views
def service_view(db_path) -> dict:
    c = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5)
    try:
        alerts = [{"name": n, "ts": ts, "text": t, "severity": severity(n), "service": needs_service(n, t)}
                  for n, ts, t in c.execute("SELECT name, ts, user_text FROM car_alerts ORDER BY ts DESC")]
        visits = []
        for s, d in c.execute("SELECT summary, detail FROM service_visits"):
            s = json.loads(s)
            d = json.loads(d) if d else {}
            loc = s.get("location")
            visits.append({
                "id": s.get("service_visit_id"), "number": s.get("service_visit_number"),
                "type": s.get("visit_type"), "status": s.get("status"),
                "in": s.get("checked_in_at"), "done": s.get("completed_at"),
                "location": loc.get("name") if isinstance(loc, dict) else loc,
                "work": [{"code": w.get("correction_code"), "text": w.get("description"), "pay": w.get("pay_type")}
                         for w in d.get("work_items") or []],
                "cost": d.get("cost") or {},
            })
        visits.sort(key=lambda v: v["done"] or v["in"] or "", reverse=True)
        year = str(dt.date.today().year)
        paid = lambda vs: round(sum((v["cost"].get("total") or 0) for v in vs), 2)  # noqa: E731
        sw = [{"version": v, "first_seen": t, "notes": json.loads(n or "[]")}
              for v, t, n in c.execute("SELECT version, first_seen, notes FROM software ORDER BY first_seen DESC")]
        seen = [{"version": v, "first": f, "last": l} for v, f, l in c.execute(
            "SELECT car_version, MIN(ts), MAX(ts) FROM reads WHERE car_version IS NOT NULL GROUP BY car_version ORDER BY MIN(ts) DESC")]

        def setting(k):
            r = c.execute("SELECT value FROM settings WHERE key=?", (k,)).fetchone()
            return json.loads(r[0]) if r else None
        return {
            "alerts": alerts, "visits": visits,
            "paid_total": paid(visits), "paid_year": paid([v for v in visits if (v["done"] or "").startswith(year)]),
            "software": sw, "versions_seen": seen, "staged": setting("release_staged"),
            "service_data": setting("service_data"), "drivers": setting("drivers") or [],
            "last_sync": setting("extras_last_sync"), "alerts_last_sync": setting("alerts_last_sync"),
        }
    finally:
        c.close()


def nearby_view(db_path) -> dict | None:
    c = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5)
    try:
        r = c.execute("SELECT value FROM settings WHERE key='nearby'").fetchone()
        return json.loads(r[0]) if r else None
    finally:
        c.close()
