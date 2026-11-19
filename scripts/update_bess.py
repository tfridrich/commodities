"""
Indicative battery (BESS) revenues for Austria (AT) and Germany-Luxembourg (DE_LU).

For every delivery day, and for a 1-hour, 2-hour and 4-hour battery of 1 MW, a linear
program finds the best schedule with perfect hindsight of that day's prices:

  da      day-ahead arbitrage only (SDAC prices, hourly until Sep 2025, 15-minute after)
  fcr     FCR capacity only
  afrr    aFRR capacity only (up and down)
  stack   FCR + aFRR capacity + day-ahead arbitrage, co-optimised in 4-hour blocks
  id_*    optional, Germany only: an intraday re-optimisation on top of the day-ahead
          schedule (merchant) or of the stacked schedule, against the Netztransparenz
          ID-AEP quarter-hour index. Needs NTP_CLIENT_ID and NTP_CLIENT_SECRET.

Inputs are the files that scripts/update_tb.py keeps up to date:
  docs/data/prices/<zone>_<year>.json   day-ahead prices
  docs/data/balancing_daily.json        daily average FCR / aFRR capacity prices (EUR/MW/h)
Output: docs/data/bess_daily.json (EUR per MW of power per day, plus cycles per day).

Usage:
    python scripts/update_bess.py                     # recompute recent and missing days
    python scripts/update_bess.py --since 2024-01-01  # recompute from a date
Needs numpy and scipy (pip install numpy scipy).
"""
import argparse
import csv
import datetime as dt
import io
import json
import os
import sys
import urllib.parse
import urllib.request
from collections import defaultdict
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
from scipy.optimize import linprog
from scipy.sparse import lil_matrix

MARKET_TZ = ZoneInfo("Europe/Vienna")
UTC = dt.timezone.utc
DATA_DIR = Path(__file__).resolve().parent.parent / "docs" / "data"
PRICE_DIR = DATA_DIR / "prices"
BAL_FILE = DATA_DIR / "balancing_daily.json"
ID_FILE = DATA_DIR / "id_aep_DE_LU.json"
OUT_FILE = DATA_DIR / "bess_daily.json"
ZONES = ["AT", "DE_LU"]
DA_START = dt.date(2019, 1, 1)
REFRESH_DAYS = 10

# ---- battery and market assumptions (shown on the web page)
CONFIG = {
    "power_mw": 1.0,
    "durations_h": [1, 2, 4],
    "round_trip_efficiency": 0.86,      # AC-AC, split equally between charging and discharging
    "max_cycles_per_day": 1.5,          # full equivalent cycles (discharged energy / capacity)
    "soc_start_end": 0.5,               # each day starts and ends half full
    "throughput_cost_eur_mwh": 1.0,     # small wear cost per MWh discharged, avoids pointless cycling
    "fcr_power_factor": 1.25,           # MW of power held per MW of FCR (headroom for SoC management)
    "fcr_energy_h": 0.25,               # FCR: 15 minutes full activation in each direction
    "afrr_energy_h": 1.0,               # aFRR: 1 hour full activation in the offered direction
    "block_h": 4,                       # FCR and aFRR capacity products are 4-hour blocks
}
COLUMNS = ["date", "da", "fcr", "afrr", "stack_da", "stack_fcr", "stack_afrr_up", "stack_afrr_down",
           "cycles_da", "cycles_stack", "id_merchant", "id_stack"]


# ---------------------------------------------------------------- inputs

def load_prices(zone):
    out = {}
    for f in sorted(PRICE_DIR.glob(f"{zone}_*.json")):
        out.update(json.loads(f.read_text())["days"])
    return out


def local_hours(day, n, minutes):
    """Local clock hour (0-23) at the start of each of the n periods of a delivery day."""
    start = dt.datetime.combine(day, dt.time(0), MARKET_TZ).astimezone(UTC)
    return [(start + dt.timedelta(minutes=minutes * i)).astimezone(MARKET_TZ).hour for i in range(n)]


def load_balancing():
    if not BAL_FILE.exists():
        return {}
    b = json.loads(BAL_FILE.read_text())
    cols = b["columns"]
    out = {}
    for zone, rows in b["markets"].items():
        out[zone] = {r[0]: dict(zip(cols[1:], r[1:])) for r in rows}
    return out


# ---------------------------------------------------------------- intraday (optional, Germany)

def ntp_token(cid, secret):
    data = urllib.parse.urlencode({"grant_type": "client_credentials", "client_id": cid,
                                   "client_secret": secret}).encode()
    with urllib.request.urlopen("https://identity.netztransparenz.de/users/connect/token", data, timeout=60) as r:
        return json.loads(r.read())["access_token"]


def parse_id_aep(text):
    """Netztransparenz CSV: Datum;von;Zeitzone von;bis;Zeitzone bis;ID AEP in EUR/MWh."""
    out = {}
    rows = csv.reader(io.StringIO(text.lstrip("﻿")), delimiter=";")
    next(rows, None)  # header
    for r in rows:
        if len(r) < 6 or not r[5].strip():
            continue
        try:
            d = dt.date.fromisoformat(r[0].strip()) if "-" in r[0] else dt.datetime.strptime(r[0].strip(), "%d.%m.%Y").date()
            hh, mm = map(int, r[1].strip().split(":"))
            tz = UTC if r[2].strip().upper() == "UTC" else MARKET_TZ
            t = dt.datetime.combine(d, dt.time(hh, mm), tz).astimezone(UTC)
            v = r[5].strip()
            v = v.replace(".", "").replace(",", ".") if "," in v else v
            out[t.strftime("%Y-%m-%dT%H:%MZ")] = round(float(v), 2)
        except ValueError:
            continue
    return out


def update_id_aep(first, last):
    """Fetch ID-AEP into docs/data/id_aep_DE_LU.json. Returns True if credentials were present."""
    cid, secret = os.environ.get("NTP_CLIENT_ID"), os.environ.get("NTP_CLIENT_SECRET")
    if not (cid and secret):
        return False
    store = json.loads(ID_FILE.read_text()) if ID_FILE.exists() else {"unit": "EUR/MWh", "slots": {}}
    token = ntp_token(cid, secret)
    s = first
    while s <= last:
        e = min(last, s + dt.timedelta(days=30))
        a = dt.datetime.combine(s, dt.time(0), MARKET_TZ).astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S")
        b = dt.datetime.combine(e + dt.timedelta(days=1), dt.time(0), MARKET_TZ).astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S")
        req = urllib.request.Request(f"https://ds.netztransparenz.de/api/v1/data/IdAep/{a}/{b}",
                                     headers={"Authorization": f"Bearer {token}"})
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                got = parse_id_aep(r.read().decode("utf-8", "replace"))
            store["slots"].update(got)
            print(f"  ID-AEP {s}..{e}: {len(got)} quarter-hours")
        except Exception as ex:  # keep going; the day is simply computed without intraday
            print(f"  warning: ID-AEP {s}..{e} failed: {ex}")
        s = e + dt.timedelta(days=1)
    store["slots"] = dict(sorted(store["slots"].items()))
    ID_FILE.write_text(json.dumps(store, separators=(",", ":")))
    return True


def id_prices_for_day(slots, day):
    start = dt.datetime.combine(day, dt.time(0), MARKET_TZ).astimezone(UTC)
    end = dt.datetime.combine(day + dt.timedelta(days=1), dt.time(0), MARKET_TZ).astimezone(UTC)
    out, t = [], start
    while t < end:
        v = slots.get(t.strftime("%Y-%m-%dT%H:%MZ"))
        if v is None:
            return None
        out.append(v)
        t += dt.timedelta(minutes=15)
    return out


# ---------------------------------------------------------------- optimiser

def solve_day(prices, minutes, hours, E, cap=None, reserves=True, fixed_res=None, base=None):
    """One delivery day for a 1 MW battery with E MWh.

    prices   energy price per period (EUR/MWh); zeros switch arbitrage off
    cap      dict fcr / afrr_cap_up / afrr_cap_down in EUR/MW/h, or None
    fixed_res  (f, u, w) per block to hold fixed (intraday stage)
    base     net DA position per period (MW, discharge positive) for the intraday stage;
             the objective then values only the change against it
    Returns dict with revenues (EUR/MW/day), cycles and the schedule, or None if infeasible.
    """
    c = CONFIG
    P = c["power_mw"]
    n = len(prices)
    dth = minutes / 60
    eta = c["round_trip_efficiency"] ** 0.5
    blocks = sorted(set(h // c["block_h"] for h in hours))
    bidx = {b: i for i, b in enumerate(blocks)}
    tb = [bidx[h // c["block_h"]] for h in hours]
    nb = len(blocks)
    block_hours = [sum(dth for t in range(n) if tb[t] == k) for k in range(nb)]
    # variable layout: charge[n], discharge[n], soc[n], f[nb], u[nb], w[nb]
    C, D, S = 0, n, 2 * n
    F, U, W = 3 * n, 3 * n + nb, 3 * n + 2 * nb
    nv = 3 * n + 3 * nb
    cost = np.zeros(nv)
    p = np.asarray(prices, float)
    cost[C:C + n] = p * dth
    cost[D:D + n] = -(p - c["throughput_cost_eur_mwh"]) * dth
    use_res = reserves and cap is not None
    if use_res:
        for k in range(nb):
            cost[F + k] = -cap["fcr"] * block_hours[k]
            cost[U + k] = -cap["afrr_cap_up"] * block_hours[k]
            cost[W + k] = -cap["afrr_cap_down"] * block_hours[k]
    bounds = [(0, P)] * (2 * n) + [(0, E)] * n
    if fixed_res is not None:
        bounds += [(v, v) for v in fixed_res]
    elif use_res:
        bounds += [(0, P / c["fcr_power_factor"])] * nb + [(0, P)] * (2 * nb)
    else:
        bounds += [(0, 0)] * (3 * nb)

    A = lil_matrix((6 * n + 1, nv))
    bu = np.zeros(6 * n + 1)
    Aeq = lil_matrix((n + 1, nv))
    beq = np.zeros(n + 1)
    tF, tA, fpf = c["fcr_energy_h"], c["afrr_energy_h"], c["fcr_power_factor"]
    s0 = c["soc_start_end"] * E
    r = 0
    for t in range(n):
        k = tb[t]
        # power headroom
        A[r, D + t] = 1; A[r, F + k] = fpf; A[r, U + k] = 1; bu[r] = P; r += 1
        A[r, C + t] = 1; A[r, F + k] = fpf; A[r, W + k] = 1; bu[r] = P; r += 1
        # energy headroom after the period ...
        A[r, S + t] = -1; A[r, F + k] = tF / eta; A[r, U + k] = tA / eta; bu[r] = 0; r += 1
        A[r, S + t] = 1; A[r, F + k] = tF * eta; A[r, W + k] = tA * eta; bu[r] = E; r += 1
        # ... and before it (the previous state of charge)
        if t == 0:
            A[r, F + k] = tF / eta; A[r, U + k] = tA / eta; bu[r] = s0; r += 1
            A[r, F + k] = tF * eta; A[r, W + k] = tA * eta; bu[r] = E - s0; r += 1
        else:
            A[r, S + t - 1] = -1; A[r, F + k] = tF / eta; A[r, U + k] = tA / eta; bu[r] = 0; r += 1
            A[r, S + t - 1] = 1; A[r, F + k] = tF * eta; A[r, W + k] = tA * eta; bu[r] = E; r += 1
        # state of charge balance
        Aeq[t, S + t] = 1
        if t > 0:
            Aeq[t, S + t - 1] = -1
        Aeq[t, C + t] = -eta * dth
        Aeq[t, D + t] = dth / eta
        beq[t] = s0 if t == 0 else 0
    # daily cycle limit
    A[r, D:D + n] = dth; bu[r] = c["max_cycles_per_day"] * E
    Aeq[n, S + n - 1] = 1; beq[n] = s0

    res = linprog(cost, A_ub=A.tocsr(), b_ub=bu, A_eq=Aeq.tocsr(), b_eq=beq, bounds=bounds, method="highs")
    if res.status != 0:
        return None
    x = res.x
    ch, dis = x[C:C + n], x[D:D + n]
    net = dis - ch
    f, u, w = x[F:F + nb], x[U:U + nb], x[W:W + nb]
    out = {"net": net, "res": list(f) + list(u) + list(w),
           "cycles": float(dis.sum() * dth / E)}
    if base is None:
        out["energy"] = float((net * p).sum() * dth)
    else:
        out["energy"] = float(((net - base) * p).sum() * dth)
    if cap is not None:
        out["fcr"] = float(sum(f[k] * cap["fcr"] * block_hours[k] for k in range(nb)))
        out["afrr_up"] = float(sum(u[k] * cap["afrr_cap_up"] * block_hours[k] for k in range(nb)))
        out["afrr_down"] = float(sum(w[k] * cap["afrr_cap_down"] * block_hours[k] for k in range(nb)))
    return out


def to_quarter_hours(values, minutes):
    rep = minutes // 15
    return np.repeat(np.asarray(values, float), rep)


def day_results(day, minutes, prices, cap, E, id_prices=None):
    hours = local_hours(day, len(prices), minutes)
    rnd = lambda v: None if v is None else round(v, 1)
    row = {k: None for k in COLUMNS[1:]}

    da = solve_day(prices, minutes, hours, E, reserves=False)
    if da:
        row["da"], row["cycles_da"] = rnd(da["energy"]), round(da["cycles"], 2)
    if cap and all(cap.get(k) is not None for k in ("fcr", "afrr_cap_up", "afrr_cap_down")):
        zero = [0.0] * len(prices)
        fcr_only = solve_day(zero, minutes, hours, E, cap={"fcr": cap["fcr"], "afrr_cap_up": 0, "afrr_cap_down": 0})
        afrr_only = solve_day(zero, minutes, hours, E, cap={"fcr": 0, "afrr_cap_up": cap["afrr_cap_up"],
                                                            "afrr_cap_down": cap["afrr_cap_down"]})
        st = solve_day(prices, minutes, hours, E, cap=cap)
        if fcr_only:
            row["fcr"] = rnd(fcr_only["fcr"])
        if afrr_only:
            row["afrr"] = rnd(afrr_only["afrr_up"] + afrr_only["afrr_down"])
        if st:
            row.update(stack_da=rnd(st["energy"]), stack_fcr=rnd(st["fcr"]), stack_afrr_up=rnd(st["afrr_up"]),
                       stack_afrr_down=rnd(st["afrr_down"]), cycles_stack=round(st["cycles"], 2))
    else:
        st = None

    # intraday stage: re-trade the physical schedule against ID-AEP, DA position and reserves fixed
    if id_prices is not None and len(id_prices) == len(prices) * minutes // 15:
        qh = local_hours(day, len(id_prices), 15)
        if da:
            r = solve_day(id_prices, 15, qh, E, reserves=False, base=to_quarter_hours(da["net"], minutes))
            if r:
                row["id_merchant"] = rnd(r["energy"])
        if st:
            r = solve_day(id_prices, 15, qh, E, cap=cap, fixed_res=st["res"], base=to_quarter_hours(st["net"], minutes))
            if r:
                row["id_stack"] = rnd(r["energy"])
    return row


# ---------------------------------------------------------------- main

def work(args):
    zone, ds, minutes, prices, cap, id_prices = args
    day = dt.date.fromisoformat(ds)
    return zone, ds, {f"{E}h": day_results(day, minutes, prices, cap, E, id_prices) for E in CONFIG["durations_h"]}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", help="recompute from this date (YYYY-MM-DD)")
    ap.add_argument("--workers", type=int, default=os.cpu_count() or 1)
    a = ap.parse_args()

    out = json.loads(OUT_FILE.read_text()) if OUT_FILE.exists() else {}
    if out.get("config") != CONFIG or out.get("columns") != COLUMNS:
        out = {}  # assumptions changed: rebuild everything
    out.setdefault("markets", {})
    bal = load_balancing()

    today = dt.datetime.now(UTC).astimezone(MARKET_TZ).date()
    has_id = update_id_aep(
        dt.date.fromisoformat(a.since) if a.since else today - dt.timedelta(days=REFRESH_DAYS),
        today) if "DE_LU" in ZONES else False
    id_slots = json.loads(ID_FILE.read_text())["slots"] if ID_FILE.exists() else {}

    jobs = []
    for zone in ZONES:
        prices = load_prices(zone)
        store = out["markets"].setdefault(zone, {f"{E}h": [] for E in CONFIG["durations_h"]})
        done = {r[0] for r in store[f"{CONFIG['durations_h'][0]}h"]}
        if a.since:
            first = dt.date.fromisoformat(a.since)
        elif done:
            first = dt.date.fromisoformat(max(done)) - dt.timedelta(days=REFRESH_DAYS)
        else:
            first = DA_START
        for ds, (minutes, p) in sorted(prices.items()):
            d = dt.date.fromisoformat(ds)
            if d < DA_START or (d < first and ds in done):
                continue
            cap = bal.get(zone, {}).get(ds)
            idp = id_prices_for_day(id_slots, d) if zone == "DE_LU" and id_slots else None
            jobs.append((zone, ds, minutes, p, cap, idp))
    print(f"BESS model: {len(jobs)} zone-days to compute (intraday {'on' if has_id or id_slots else 'off'})")

    if a.workers > 1 and len(jobs) > 50:
        from multiprocessing import Pool
        with Pool(a.workers) as pool:
            results = pool.map(work, jobs, chunksize=20)
    else:
        results = [work(j) for j in jobs]

    for zone in ZONES:
        for key in out["markets"][zone]:
            rows = {r[0]: r for r in out["markets"][zone][key]}
            for z, ds, res in results:
                if z == zone:
                    rows[ds] = [ds] + [res[key][c] for c in COLUMNS[1:]]
            out["markets"][zone][key] = [rows[d] for d in sorted(rows)]
    out["config"] = CONFIG
    out["columns"] = COLUMNS
    out["units"] = "EUR per MW of power per delivery day; cycles = full equivalent cycles per day"
    out["updated"] = dt.datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    OUT_FILE.write_text(json.dumps(out, separators=(",", ":")))
    print(f"wrote {OUT_FILE.relative_to(DATA_DIR.parent.parent)}")


if __name__ == "__main__":
    main()
