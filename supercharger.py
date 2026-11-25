"""Supercharger history from Tesla (GET /api/1/dx/charging/history) and what it would have cost.

With free unlimited Supercharging every CHARGING fee is pricingType NO_CHARGE - but Tesla still
records the real site rate (rateBase, $/kWh at that site at that time, i.e. time-of-use is
already in it) and the kWh delivered (usageBase). Value of a session:

    charging   = rateBase * kWh                                   (Tesla's own numbers)
    congestion = CONGESTION.rateBase * minutes Tesla recorded      (actual; 0 in all history so far)
               + estimate from our own readings when we watched the session: minutes from the car
                 reaching 80% until charging stopped, at a site that had a congestion fee then
                 ("if the site was busy" - Tesla charges it only when busy)
    idle       = PARKING fees, which free Supercharging does NOT cover: shown as money actually paid

usageTier1/2 on CHARGING are minutes in each power tier (rates are 0 because billing is per kWh);
their sum is the charging time.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import sqlite3
import time

log = logging.getLogger("tesla.sc")
CONGESTION_FROM_PCT = 80


def sync(api, c: sqlite3.Connection, vin: str, full: bool = False) -> int:
    """Fetch newest-first pages until a known session shows up. Returns sessions added."""
    known = {r[0] for r in c.execute("SELECT session_id FROM sc_sessions")}
    added, page = 0, 1
    while page <= 40:
        code, body = api.request(f"/api/1/dx/charging/history?vin={vin}&pageSize=50&pageNo={page}", "charging_history")
        if code != 200 or not body:
            log.warning("charging history page %s: HTTP %s", page, code)
            break
        data = body.get("data") or []
        seen_known = False
        for s in data:
            sid = str(s["sessionId"])
            seen_known |= sid in known
            c.execute("INSERT OR REPLACE INTO sc_sessions VALUES(?,?,?)", (sid, s.get("chargeStartDateTime"), json.dumps(s)))
            added += sid not in known
        c.commit()
        if len(data) < 50 or (seen_known and not full):
            break
        page += 1
    c.execute("INSERT INTO settings VALUES('sc_last_sync', ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
              (json.dumps(time.time()),))
    c.commit()
    return added


def _ts(iso: str | None) -> float | None:
    if not iso:
        return None
    try:
        return dt.datetime.fromisoformat(iso).timestamp()
    except ValueError:
        return None


def _fee(s: dict, kind: str) -> dict | None:
    return next((f for f in s.get("fees", []) if f.get("feeType") == kind), None)


def _congestion_estimate(c, start: float, stop: float) -> float | None:
    """Minutes charging at >= 80% from our own readings; None if we didn't watch this session."""
    rows = c.execute("SELECT ts, battery_level, charging_state FROM reads WHERE ts BETWEEN ? AND ? "
                     "AND battery_level IS NOT NULL ORDER BY ts", (start - 600, stop + 600)).fetchall()
    rows = [r for r in rows if r[2] in ("Charging", "Starting", "Complete", "Stopped")]
    if not rows:
        return None
    prev = None
    for ts, pct, _ in rows:
        if pct >= CONGESTION_FROM_PCT:
            if prev and prev[1] < CONGESTION_FROM_PCT and pct > prev[1]:      # interpolate the crossing
                ts = prev[0] + (ts - prev[0]) * (CONGESTION_FROM_PCT - prev[1]) / (pct - prev[1])
            return max(0.0, (stop - max(ts, start)) / 60)
        prev = (ts, pct)
    return 0.0


def sessions(db_path) -> list[dict]:
    c = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5)
    try:
        out = []
        for (raw,) in c.execute("SELECT raw FROM sc_sessions ORDER BY start DESC"):
            s = json.loads(raw)
            ch, cg, pk = _fee(s, "CHARGING") or {}, _fee(s, "CONGESTION"), _fee(s, "PARKING")
            start, stop, unlatch = _ts(s.get("chargeStartDateTime")), _ts(s.get("chargeStopDateTime")), _ts(s.get("unlatchDateTime"))
            kwh = ch.get("usageBase") or 0.0
            rate = ch.get("rateBase") or 0.0
            mins = (ch.get("usageTier1") or 0) + (ch.get("usageTier2") or 0) + (ch.get("usageTier3") or 0) + (ch.get("usageTier4") or 0)
            if not mins and start and stop:
                mins = (stop - start) / 60
            cg_rate = (cg or {}).get("rateBase") or 0.0
            cg_min = ((cg or {}).get("usageBase") or 0) + ((cg or {}).get("usageTier1") or 0) + ((cg or {}).get("usageTier2") or 0)
            est_min = _congestion_estimate(c, start, stop) if cg and cg_rate and start and stop else None
            idle_min = ((pk or {}).get("usageBase") or 0) if pk else 0
            idle_paid = ((pk or {}).get("totalDue") or 0) if pk and pk.get("pricingType") == "PAYMENT" else 0
            out.append({
                "id": str(s.get("sessionId")), "site": (s.get("siteLocationName") or "").replace("  ", " ").strip(),
                "start": start, "stop": stop, "unlatch": unlatch,
                "kwh": round(kwh, 2), "rate": rate, "free": ch.get("pricingType") == "NO_CHARGE",
                "charge_value": round(rate * kwh, 2), "charged": ch.get("totalDue") or 0,
                "minutes": round(mins), "avg_kw": round(kwh / (mins / 60), 1) if mins else None,
                "congestion_rate": cg_rate if cg else None,
                "congestion_min": cg_min, "congestion_value": round(cg_min * cg_rate, 2),
                "congestion_est_min": round(est_min) if est_min is not None else None,
                "congestion_est_value": round(est_min * cg_rate, 2) if est_min else 0,
                "idle_min": idle_min, "idle_paid": idle_paid,
                "value": round(rate * kwh + cg_min * cg_rate, 2),
                "currency": ch.get("currencyCode") or "USD",
            })
        return out
    finally:
        c.close()


def summary(db_path, home_rate: float = 0.24, loss_pct: float = 10.0) -> dict:
    """home_cost = what the same kWh would cost charging at home: wall kWh (battery kWh / (1 - loss)) * rate."""
    ss = sessions(db_path)
    per_kwh = home_rate / (1 - min(loss_pct, 90) / 100)
    for r in ss:
        r["home_cost"] = round(r["kwh"] * per_kwh, 2)
    now = dt.datetime.now()

    def total(rows):
        return {"sessions": len(rows), "kwh": round(sum(r["kwh"] for r in rows), 1),
                "value": round(sum(r["value"] for r in rows), 2),
                "congestion_recorded": round(sum(r["congestion_value"] for r in rows), 2),
                "congestion_estimated": round(sum(r["congestion_est_value"] for r in rows), 2),
                "idle_paid": round(sum(r["idle_paid"] for r in rows), 2),
                "home_cost": round(sum(r["kwh"] for r in rows) * per_kwh, 2),
                "avg_rate": round(sum(r["charge_value"] for r in rows) / max(0.01, sum(r["kwh"] for r in rows)), 3)}

    year = [r for r in ss if r["start"] and dt.datetime.fromtimestamp(r["start"]).year == now.year]
    month = [r for r in year if dt.datetime.fromtimestamp(r["start"]).month == now.month]
    by_month: dict[str, list] = {}
    for r in ss:
        if r["start"]:
            by_month.setdefault(dt.datetime.fromtimestamp(r["start"]).strftime("%Y-%m"), []).append(r)
    by_year: dict[int, list] = {}
    for r in ss:
        if r["start"]:
            by_year.setdefault(dt.datetime.fromtimestamp(r["start"]).year, []).append(r)
    sites: dict[str, list] = {}
    for r in ss:
        sites.setdefault(r["site"], []).append(r)
    site_rows = []
    for name, rows in sites.items():
        rates = [r["rate"] for r in rows if r["rate"]]
        site_rows.append({"site": name, **total(rows), "rate_min": min(rates) if rates else None,
                          "rate_max": max(rates) if rates else None, "rate_latest": rows[0]["rate"],
                          "last": rows[0]["start"], "has_congestion": any(r["congestion_rate"] for r in rows)})
    site_rows.sort(key=lambda x: -x["value"])
    first = min((r["start"] for r in ss if r["start"]), default=None)
    return {
        "lifetime": total(ss), "year": total(year), "month": total(month), "since": first,
        "by_month": [[k, round(sum(r["value"] for r in v), 2), round(sum(r["kwh"] for r in v), 1), len(v)]
                     for k, v in sorted(by_month.items())],
        "months": [[k, total(v)] for k, v in sorted(by_month.items())],
        "home_rate": home_rate, "home_loss_pct": loss_pct, "home_per_kwh": round(per_kwh, 4),
        "by_year": [[k, total(v)] for k, v in sorted(by_year.items())],
        "sites": site_rows,
        "sessions": ss[:400],
        "session_count": len(ss),
    }
