"""Charging sessions, rebuilt on request from the poller's saved car readings.

A session starts at a reading that says Charging/Starting and runs through the following
plugged-in readings. It ends at a Disconnected reading, a gap of more than 12 h, or when the
car's own session counter (charge_energy_added) resets. The car is only read once it has stayed
awake ~30 min, so the start time and start % can be late; kWh added is the car's own total
for the plug-in and is exact.
"""
from __future__ import annotations

import datetime as dt
import gzip
import json
import math
import pathlib
import sqlite3

import config

PLACES_DB = config.CONFIG["places_db"]   # optional Family Locator DB: geofences -> place names
GAP_SPLIT = 12 * 3600


def _places() -> list[tuple[str, float, float, float]]:
    if not PLACES_DB:
        return []
    try:
        con = sqlite3.connect(f"file:{PLACES_DB}?mode=ro", uri=True, timeout=3)
        rows = con.execute("SELECT name, lat, lon, radius FROM geofences").fetchall()
        con.close()
        return rows
    except sqlite3.Error:
        return []


def _metres(a_lat, a_lon, b_lat, b_lon) -> float:
    p = math.pi / 180
    h = (math.sin((b_lat - a_lat) * p / 2) ** 2 +
         math.cos(a_lat * p) * math.cos(b_lat * p) * math.sin((b_lon - a_lon) * p / 2) ** 2)
    return 12_742_000 * math.asin(math.sqrt(h))


def _place(lat, lon, places) -> str | None:
    if lat is None:
        return None
    best = None
    for name, plat, plon, rad in places:
        d = _metres(lat, lon, plat, plon)
        if d <= rad + 50 and (best is None or d < best[0]):
            best = (d, name)
    return best[1] if best else None


def sessions(db_path: pathlib.Path) -> list[dict]:
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5)
    con.row_factory = sqlite3.Row
    rows = con.execute("SELECT ts, battery_level, charging_state, charger_power, charge_energy_added, "
                       "latitude, longitude, raw FROM reads ORDER BY ts").fetchall()
    con.close()
    places = _places()
    out, cur, prev_ts = [], None, None

    def close():
        nonlocal cur
        if cur and cur["charging_reads"]:
            out.append(cur)
        cur = None

    for r in rows:
        st = r["charging_state"]
        charging = st in ("Charging", "Starting")
        plugged = st not in (None, "Disconnected")
        kwh = r["charge_energy_added"]
        if cur and (not plugged or (prev_ts and r["ts"] - prev_ts > GAP_SPLIT)
                    or (kwh is not None and cur["kwh"] is not None and kwh < cur["kwh"] - 0.5)):
            # The first reading after unplugging still carries the plug-in's final totals.
            if not plugged and prev_ts and r["ts"] - prev_ts <= GAP_SPLIT:
                if kwh is not None and kwh >= (cur["kwh"] or 0):
                    cur["kwh"] = kwh
                if r["battery_level"] is not None:
                    cur["end_pct"] = max(cur["end_pct"] or 0, r["battery_level"])
            close()
        prev_ts = r["ts"]
        if not plugged or (cur is None and not charging):
            continue
        cs = json.loads(gzip.decompress(r["raw"])).get("charge_state", {}) if r["raw"] else {}
        if cur is None:
            cur = {"start": r["ts"], "end": r["ts"], "start_pct": r["battery_level"], "end_pct": r["battery_level"],
                   "kwh": kwh, "peak_kw": 0.0, "kw_sum": 0.0, "charging_reads": 0, "max_v": 0, "max_a": 0,
                   "fast": False, "brand": None, "lat": r["latitude"], "lon": r["longitude"],
                   "state": st, "limit": cs.get("charge_limit_soc")}
        cur["end"], cur["state"] = r["ts"], st
        if r["battery_level"] is not None:
            cur["end_pct"] = max(cur["end_pct"] or 0, r["battery_level"])
        if kwh is not None:
            cur["kwh"] = max(cur["kwh"] or 0, kwh)
        if charging:
            kw = r["charger_power"] or 0
            cur["charging_reads"] += 1
            cur["peak_kw"] = max(cur["peak_kw"], kw)
            cur["kw_sum"] += kw
            cur["max_v"] = max(cur["max_v"], cs.get("charger_voltage") or 0)
            cur["max_a"] = max(cur["max_a"], cs.get("charger_actual_current") or 0)
        if cs.get("fast_charger_present"):
            cur["fast"] = True
            b = cs.get("fast_charger_brand")
            cur["brand"] = b if b and b != "<invalid>" else cur["brand"]
        if cur["lat"] is None:
            cur["lat"], cur["lon"] = r["latitude"], r["longitude"]
    if cur and cur["charging_reads"]:
        cur["ongoing"] = cur["state"] in ("Charging", "Starting")
        out.append(cur)
    for s in out:
        s.setdefault("ongoing", False)
        s["avg_kw"] = round(s.pop("kw_sum") / s["charging_reads"], 1) if s["charging_reads"] else None
        s["place"] = _place(s["lat"], s["lon"], places)
        s["kind"] = ("Supercharger" if s["brand"] in (None, "Tesla") else s["brand"]) if s["fast"] else "AC"
    return list(reversed(out))
