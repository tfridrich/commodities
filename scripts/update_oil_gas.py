"""
Daily update of crude oil and natural gas prices for the oil-gas page (docs/oil-gas/).

Sources
  - Yahoo Finance chart API (no key): daily closes of front-month futures
        Brent      BZ=F   USD/bbl     NYMEX Brent Last Day Financial (cash-settled on ICE Brent)
        WTI        CL=F   USD/bbl     NYMEX Light Sweet Crude
        Henry Hub  NG=F   USD/MMBtu   NYMEX Henry Hub Natural Gas
        TTF        TTF=F  EUR/MWh     NYMEX Dutch TTF (cash-settled on ICE Endex TTF)
        NBP        UKG=F  GBp/therm   NYMEX UK NBP (cash-settled on ICE NBP)
    plus EUR/USD and GBP/USD reference rates for the unit conversions on the page, and the
    forward curves of WTI, Brent and Henry Hub from the individual contract months
    (docs/data/oil_gas_curves.json; see CURVES).
  - EIA Open Data API v2 (optional, free key in EIA_API_KEY): official daily spot prices
        Brent spot (RBRTE), WTI Cushing spot (RWTC), Henry Hub spot (RNGWHHD).
    EIA publishes these once a week with a few days' delay; they are US public-domain data.

To add a benchmark, add one entry to SERIES (Yahoo symbol or EIA series id) and, if it is
a new unit, one line to UNIT_INFO on the page. Everything else picks it up automatically.

Usage:
    python scripts/update_oil_gas.py            # refresh the last REFRESH_DAYS days
    python scripts/update_oil_gas.py --full     # rebuild the full history
    python scripts/update_oil_gas.py --no-curves     # prices only, skip the forward curves
    python scripts/update_oil_gas.py --curves-only   # forward curves only
    EIA_API_KEY=... python scripts/update_oil_gas.py   # also fetch the EIA spot series

Caching and fallbacks (Yahoo blocks many cloud servers, including GitHub's):
  - Only days that are missing are requested. A series whose last stored day is already the
    latest settled trading day is not requested at all, and neither is a forward curve that
    already has that day. Repeated runs therefore cost almost no Yahoo requests.
  - EUR/USD and GBP/USD come from the ECB (official reference rates), Yahoo only as fallback.
  - When Yahoo refuses, WTI and Henry Hub front-month settlements are filled in from the EIA
    (NYMEX contract 1, series RCLC1 and RNGC1), if EIA_API_KEY is set. EIA lags a few days.
  - Whatever is still missing (Brent, TTF, NBP, forward curves) is filled in by the next run
    that gets through to Yahoo, e.g. the scheduled task on Thomas's PC (local_runner/).

Only the Python standard library is used.
"""
import argparse
import datetime as dt
import json
import re
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from zoneinfo import ZoneInfo

# Use the operating system's certificate store when the optional 'truststore' package is
# installed (fixes "CERTIFICATE_VERIFY_FAILED" with Anaconda on Windows, e.g. for the ECB).
try:
    import truststore
    truststore.inject_into_ssl()
except Exception:
    pass

OUT_FILE = Path(__file__).resolve().parent.parent / "docs" / "data" / "oil_gas.json"
HISTORY_START = dt.date(2000, 1, 1)
REFRESH_DAYS = 7      # re-read the last week when a series is fetched, to pick up late corrections
SETTLED_HOUR = 17     # a bar dated today counts as final after 17:00 exchange time

SERIES = [
    # key, group, label, unit, source, id, description
    dict(key="brent", group="oil", label="Brent", unit="USD/bbl", source="yahoo", id="BZ=F",
         desc="Brent crude, front-month futures (NYMEX Brent Last Day Financial, settles on ICE Brent)"),
    dict(key="wti", group="oil", label="WTI", unit="USD/bbl", source="yahoo", id="CL=F",
         desc="WTI crude, front-month NYMEX futures (Cushing, Oklahoma)"),
    dict(key="ttf", group="gas", label="TTF", unit="EUR/MWh", source="yahoo", id="TTF=F",
         desc="Dutch TTF natural gas, front-month futures (NYMEX, settles on ICE Endex TTF)"),
    dict(key="nbp", group="gas", label="NBP", unit="GBp/therm", source="yahoo", id="UKG=F",
         desc="UK NBP natural gas, front-month futures (NYMEX, settles on ICE NBP)"),
    dict(key="hh", group="gas", label="Henry Hub", unit="USD/MMBtu", source="yahoo", id="NG=F",
         desc="Henry Hub natural gas, front-month NYMEX futures (Louisiana)"),
    # Official spot prices, only fetched when EIA_API_KEY is set
    dict(key="brent_spot", group="oil", label="Brent spot (EIA)", unit="USD/bbl", source="eia",
         id="RBRTE", route="petroleum/pri/spt", desc="Europe Brent spot price FOB, EIA"),
    dict(key="wti_spot", group="oil", label="WTI spot (EIA)", unit="USD/bbl", source="eia",
         id="RWTC", route="petroleum/pri/spt", desc="Cushing, OK WTI spot price FOB, EIA"),
    dict(key="hh_spot", group="gas", label="Henry Hub spot (EIA)", unit="USD/MMBtu", source="eia",
         id="RNGWHHD", route="natural-gas/pri/fut", desc="Henry Hub natural gas spot price, EIA"),
    # Monthly LNG prices (free and public; daily LNG benchmarks such as JKM are licensed)
    dict(key="lng_asia", group="gas", label="LNG Asia (IMF monthly)", unit="USD/MMBtu", source="fred",
         id="PNGASJPUSDM", freq="monthly",
         desc="IMF price of LNG in Asia: Japan's average LNG import price, mostly long-term oil-linked contracts; monthly, published one to two months later"),
    dict(key="lng_us", group="gas", label="US LNG exports (EIA monthly)", unit="USD/MMBtu", source="eia_series",
         id="NG.N9133US3.M", scale=1 / 1.037, freq="monthly",
         desc="Average price of US LNG exports at the export terminal (EIA), converted from USD per thousand cubic feet at 1.037 MMBtu/Mcf; monthly, published a few months later"),
]
FX = [dict(key="EURUSD", id="EURUSD=X"), dict(key="GBPUSD", id="GBPUSD=X")]

# When Yahoo refuses, these front-month series are filled from the same NYMEX contract on EIA
EIA_FILL = {
    "wti": dict(route="petroleum/pri/fut", id="RCLC1"),     # NYMEX WTI contract 1, USD/bbl
    "hh": dict(route="natural-gas/pri/fut", id="RNGC1"),    # NYMEX Henry Hub contract 1, USD/MMBtu
}
ECB_URL = ("https://data-api.ecb.europa.eu/service/data/EXR/D.USD+GBP.EUR.SP00.A"
           "?format=csvdata&detail=dataonly&startPeriod={start}")
EXCHANGE_TZ = ZoneInfo("America/New_York")

# Forward curves: daily settlements of the individual NYMEX contract months on Yahoo
# (symbol = root + month code + 2-digit year + ".NYM", e.g. CLF27.NYM = WTI January 2027).
# `monthly` = number of consecutive contract months requested, `dec_years` = how many years
# ahead the December contracts are requested beyond that. Months that are not listed are skipped.
CURVE_FILE = OUT_FILE.parent / "oil_gas_curves.json"
CURVES = [
    dict(key="wti", group="oil", label="WTI", unit="USD/bbl", root="CL", monthly=72, dec_years=12,
         desc="NYMEX WTI Light Sweet Crude futures (CL)"),
    dict(key="brent", group="oil", label="Brent", unit="USD/bbl", root="BZ", monthly=48, dec_years=8,
         desc="NYMEX Brent Last Day Financial futures (BZ), settle on ICE Brent"),
    dict(key="hh", group="gas", label="Henry Hub", unit="USD/MMBtu", root="NG", monthly=84, dec_years=12,
         desc="NYMEX Henry Hub Natural Gas futures (NG)"),
]
MONTH_CODES = "FGHJKMNQUVXZ"
CURVE_BACKFILL_DAYS = 400    # first run: rebuild about a year of past curves
CURVE_REFRESH_DAYS = 10      # later runs: re-read the last 10 days
CURVE_KEEP_DAILY_DAYS = 40   # keep every day's curve for 40 days, then one per month (last trading day)

YAHOO_HOSTS = ["https://query1.finance.yahoo.com", "https://query2.finance.yahoo.com"]
EIA_URL = "https://api.eia.gov/v2/{route}/data/"
UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"


# ---------------------------------------------------------------- download helpers

class SourceBlocked(RuntimeError):
    """A data source refused several requests in a row - stop asking it for this run."""


FAILS_IN_ROW = {}          # host -> consecutive failed requests
MAX_FAILS_IN_ROW = 6       # then the host is treated as blocking this run


def get_json(url, tries=2):
    """GET a JSON document; one retry after 5 s. 404 is raised at once. When a host has failed
    MAX_FAILS_IN_ROW requests in a row (e.g. HTTP 429 'Too Many Requests'), SourceBlocked is
    raised so the run stops quickly instead of retrying for hours."""
    host = ".".join(urllib.parse.urlsplit(url).hostname.split(".")[-2:])   # e.g. yahoo.com
    if FAILS_IN_ROW.get(host, 0) >= MAX_FAILS_IN_ROW:
        raise SourceBlocked(f"{host} is refusing requests in this run - skipped")
    last = None
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=30) as r:
                body = json.loads(r.read().decode("utf-8"))
            FAILS_IN_ROW[host] = 0
            return body
        except urllib.error.HTTPError as e:
            last = e
            if e.code == 404:
                FAILS_IN_ROW[host] = 0
                raise
            if e.code in (400, 401, 403):
                break
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
            last = e
        if i < tries - 1:
            time.sleep(5)
    FAILS_IN_ROW[host] = FAILS_IN_ROW.get(host, 0) + 1
    if FAILS_IN_ROW[host] >= MAX_FAILS_IN_ROW:
        raise SourceBlocked(f"{host} refused {MAX_FAILS_IN_ROW} requests in a row (last error: {last})")
    raise last


def epoch(d):
    return int(dt.datetime.combine(d, dt.time(0), dt.timezone.utc).timestamp())


def parse_yahoo(payload, now_utc=None):
    """Turn a Yahoo chart response into {date: close} with final daily bars only.

    Yahoo stamps finished daily bars at midnight exchange time (half-day sessions such as
    24 Dec at 09:30) and the running session with the time of its last trade. The running
    session - a last bar that is not stamped at midnight - and any bar for today before
    the settlement time are left out, so that only settled prices are stored. Bars dropped
    here are picked up by a later run, which re-reads the last REFRESH_DAYS days."""
    res = payload["chart"]["result"][0]
    tz = ZoneInfo(res["meta"].get("exchangeTimezoneName") or "America/New_York")
    now_local = (now_utc or dt.datetime.now(dt.timezone.utc)).astimezone(tz)
    stamps = res.get("timestamp") or []
    closes = (res.get("indicators", {}).get("quote") or [{}])[0].get("close") or []
    out = {}
    for i, (ts, c) in enumerate(zip(stamps, closes)):
        if c is None:
            continue
        local = dt.datetime.fromtimestamp(ts, dt.timezone.utc).astimezone(tz)
        if i == len(stamps) - 1 and (local.hour, local.minute) != (0, 0):
            continue  # running session
        day = local.date()
        if day > now_local.date() or (day == now_local.date() and now_local.hour < SETTLED_HOUR):
            continue  # not settled yet
        out[day.isoformat()] = c
    return out


def fetch_yahoo(symbol, start, end, missing_ok=False, pause=1.0):
    q = urllib.parse.urlencode({"period1": epoch(start), "period2": epoch(end + dt.timedelta(days=1)),
                                "interval": "1d", "includePrePost": "false", "events": "history"})
    path = f"/v8/finance/chart/{urllib.parse.quote(symbol)}?{q}"
    err = None
    for host in YAHOO_HOSTS:
        try:
            time.sleep(pause)  # be gentle with Yahoo
            data = get_json(host + path)
            if data.get("chart", {}).get("error"):
                raise RuntimeError(data["chart"]["error"])
            return parse_yahoo(data)
        except SourceBlocked:
            raise
        except urllib.error.HTTPError as e:
            if e.code == 404 and missing_ok:
                return {}  # contract month not listed
            err = e
        except Exception as e:  # try the other host
            err = e
    raise RuntimeError(f"Yahoo {symbol}: {err}")


def fetch_eia(route, series_id, start, key):
    out, offset = {}, 0
    while True:
        params = [("api_key", key), ("frequency", "daily"), ("data[0]", "value"),
                  ("facets[series][]", series_id), ("start", start.isoformat()),
                  ("sort[0][column]", "period"), ("sort[0][direction]", "asc"),
                  ("offset", str(offset)), ("length", "5000")]
        body = get_json(EIA_URL.format(route=route) + "?" + urllib.parse.urlencode(params))["response"]
        rows = body.get("data", [])
        for r in rows:
            if r.get("value") not in (None, ""):
                out[r["period"]] = float(r["value"])
        offset += len(rows)
        if not rows or offset >= int(body.get("total", 0)):
            return out


def fetch_eia_series(series_id, key, scale=1.0):
    """A whole EIA series by its ID (e.g. NG.N9133US3.M) -> {"YYYY-MM-01": value}."""
    url = f"https://api.eia.gov/v2/seriesid/{series_id}?" + urllib.parse.urlencode({"api_key": key})
    out = {}
    for r in get_json(url)["response"].get("data", []):
        v, d = r.get("value"), str(r.get("period", ""))
        if v in (None, "") or not re.fullmatch(r"\d{4}-\d{2}(-\d{2})?", d):
            continue
        out[d if len(d) == 10 else d + "-01"] = round(float(v) * scale, 4)
    return out


def fetch_fred(series_id):
    """A whole FRED series as CSV (no key needed) -> {"YYYY-MM-DD": value}."""
    req = urllib.request.Request(f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={series_id}",
                                 headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=60) as r:
        text = r.read().decode("utf-8", errors="replace")
    lines = text.strip().splitlines()
    if not lines or series_id not in lines[0]:
        raise RuntimeError("FRED returned an unexpected page: " + text[:80].replace("\n", " "))
    out = {}
    for line in lines[1:]:
        d, _, v = line.partition(",")
        try:
            out[d.strip()] = round(float(v), 4)
        except ValueError:
            pass          # '.' marks a missing month
    return out


def last_settled_day(now_utc=None):
    """Latest weekday whose NYMEX settlement is final (after SETTLED_HOUR New York time).
    Exchange holidays are not known here; on those days one extra request is made."""
    now = (now_utc or dt.datetime.now(dt.timezone.utc)).astimezone(EXCHANGE_TZ)
    d = now.date() if now.hour >= SETTLED_HOUR else now.date() - dt.timedelta(days=1)
    while d.weekday() >= 5:
        d -= dt.timedelta(days=1)
    return d


def up_to_date(dates, settled):
    return bool(dates) and max(dates) >= settled.isoformat()


def fetch_ecb_fx(start):
    """ECB reference rates -> {"EURUSD": {date: USD per EUR}, "GBPUSD": {date: USD per GBP}}."""
    req = urllib.request.Request(ECB_URL.format(start=start.isoformat()), headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=60) as r:
        text = r.read().decode("utf-8", errors="replace")
    lines = text.strip().splitlines()
    if not lines or not lines[0].startswith("KEY,"):
        raise RuntimeError("ECB returned an unexpected page: " + text[:80].replace("\n", " "))
    head = lines[0].split(",")
    i_ccy, i_day, i_val = head.index("CURRENCY"), head.index("TIME_PERIOD"), head.index("OBS_VALUE")
    per_eur = {"USD": {}, "GBP": {}}
    for line in lines[1:]:
        f = line.split(",")
        try:
            v = float(f[i_val])
        except (ValueError, IndexError):
            continue
        if f[i_ccy] in per_eur:
            per_eur[f[i_ccy]][f[i_day]] = v
    usd = per_eur["USD"]
    return {"EURUSD": dict(usd), "GBPUSD": {d: usd[d] / v for d, v in per_eur["GBP"].items() if d in usd and v}}


# ---------------------------------------------------------------- forward curves

def add_months(year, month, n):
    k = year * 12 + month - 1 + n
    return k // 12, k % 12 + 1


def contract_months(today, monthly, dec_years, back_months=0):
    """Delivery months ('YYYY-MM') to request: `monthly` consecutive months from the current
    month (plus `back_months` before it), then the December contracts further out."""
    out = [add_months(today.year, today.month, n) for n in range(-back_months, monthly)]
    last_year = out[-1][0]
    out += [(y, 12) for y in range(last_year + (out[-1][1] == 12), today.year + dec_years + 1)]
    return [f"{y}-{m:02d}" for y, m in sorted(set(out))]


def contract_symbol(root, ym):
    y, m = int(ym[:4]), int(ym[5:])
    return f"{root}{MONTH_CODES[m - 1]}{y % 100:02d}.NYM"


def prune_snapshots(snaps, keep_days=CURVE_KEEP_DAILY_DAYS):
    """Keep every curve of the last `keep_days` days and the last curve of each earlier month."""
    if not snaps:
        return {}
    dates = sorted(snaps)
    latest = dt.date.fromisoformat(dates[-1])
    month_last = {}
    for d in dates:
        month_last[d[:7]] = d
    keep = {d for d in dates if (latest - dt.date.fromisoformat(d)).days <= keep_days}
    keep |= set(month_last.values())
    return {d: snaps[d] for d in dates if d in keep}


def build_snapshots(histories):
    """{delivery month: {date: price}} -> {date: {delivery month: price}}.
    Days on which fewer than half the usual number of contracts have a price are dropped
    (holidays or partial data), so every stored curve is complete."""
    by_date = {}
    for ym, hist in histories.items():
        for d, v in hist.items():
            if d[:7] < ym:  # contracts expire before their delivery month; later bars are stale
                by_date.setdefault(d, {})[ym] = round(v, 3)
    if not by_date:
        return {}
    sizes = sorted(len(c) for c in by_date.values())
    median = sizes[len(sizes) // 2]
    return {d: dict(sorted(c.items())) for d, c in by_date.items() if len(c) >= 0.5 * median}


def update_curves(today, full=False):
    store = json.loads(CURVE_FILE.read_text()) if CURVE_FILE.exists() else {"curves": {}}
    out, changed, blocked = {"curves": {}}, False, None
    settled = last_settled_day()
    for c in CURVES:
        old = store.get("curves", {}).get(c["key"], {})
        snaps = old.get("snapshots", {})
        if old and (blocked or (not full and up_to_date(snaps, settled))):
            out["curves"][c["key"]] = old       # cached: already has the latest settled day
            print(f"curve {c['key']:<6} " + ("kept (Yahoo is refusing)" if blocked else f"up to date ({max(snaps)})"))
            continue
        backfill = full or not snaps
        start = today - dt.timedelta(days=CURVE_BACKFILL_DAYS if backfill else CURVE_REFRESH_DAYS)
        months = contract_months(today, c["monthly"], c["dec_years"], back_months=1)
        histories = {}
        for ym in months:
            try:
                got = fetch_yahoo(contract_symbol(c["root"], ym), start, today, missing_ok=True, pause=0.3)
            except SourceBlocked as e:
                blocked = e
                break
            except Exception as e:
                print(f"::warning::{c['key']} {ym}: {e}")
                got = {}
            if got:
                histories[ym] = got
        if blocked:
            print(f"::warning::Forward curve {c['key']} not updated: {blocked}. The stored curve is kept.")
            if old:
                out["curves"][c["key"]] = old
            continue
        new = build_snapshots(histories)
        print(f"curve {c['key']:<6} {len(histories):>3} contracts, {len(new):>3} days from {start}")
        if not new:
            print(f"::warning::curve {c['key']}: no data fetched, keeping the stored curves")
        merged = {d: dict(v) for d, v in snaps.items()}
        for d, curve in new.items():
            merged[d] = dict(sorted({**merged.get(d, {}), **curve}.items()))
        merged = prune_snapshots(merged)
        meta = {k: c[k] for k in ("group", "label", "unit", "root", "desc")}
        out["curves"][c["key"]] = {**meta, "snapshots": dict(sorted(merged.items()))}
        changed |= merged != snaps or {k: old.get(k) for k in meta} != meta
    if not changed:
        print("No new forward curves - file left unchanged.")
        return not blocked
    out["updated"] = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    CURVE_FILE.write_text(json.dumps(out, separators=(",", ":")))
    print(f"Wrote {CURVE_FILE} ({CURVE_FILE.stat().st_size / 1e6:.2f} MB)")
    return not blocked


# ---------------------------------------------------------------- main

def load():
    if OUT_FILE.exists():
        return json.loads(OUT_FILE.read_text())
    return {"series": {}, "fx": {}}


def merge(old, new, digits):
    merged = dict(old or {})
    merged.update({d: round(v, digits) for d, v in new.items()})
    return dict(sorted(merged.items()))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--full", action="store_true", help="re-download the whole history")
    ap.add_argument("--no-curves", action="store_true", help="skip the forward curves")
    ap.add_argument("--curves-only", action="store_true", help="only update the forward curves")
    args = ap.parse_args()
    sys.stdout.reconfigure(line_buffering=True)   # show progress in the GitHub log as it happens
    today = dt.datetime.now(dt.timezone.utc).date()
    prices_ok = True if args.curves_only else update_prices(args, today)
    curves_ok = True if args.no_curves else update_curves(today, full=args.full)
    if not prices_ok or not curves_ok:
        print("::warning::Some prices could not be fetched (Yahoo refuses many cloud servers). "
              "They are filled in by the next run that gets through, e.g. the scheduled task on the PC.")


def update_prices(args, today):
    """Daily prices for the Crude oil and Natural gas tabs. Returns False if Yahoo refused."""
    store = load()
    eia_key = os.environ.get("EIA_API_KEY", "").strip()
    settled = last_settled_day()
    failures = []

    def start_for(existing, full_done=True):
        if args.full or not existing or not full_done:
            return HISTORY_START
        return max(HISTORY_START, dt.date.fromisoformat(max(existing)) - dt.timedelta(days=REFRESH_DAYS))

    new_series = {}
    for s in SERIES:
        old = store.get("series", {}).get(s["key"], {})
        meta = {k: s[k] for k in ("group", "label", "unit", "source", "id", "desc")}
        if s.get("freq"):
            meta["freq"] = s["freq"]
        if s["source"] in ("fred", "eia_series"):      # small monthly series: re-read whole each run
            try:
                if s["source"] == "fred":
                    got = fetch_fred(s["id"])
                elif eia_key:
                    got = fetch_eia_series(s["id"], eia_key, s.get("scale", 1.0))
                else:
                    got = {}
                if got:
                    print(f"{s['key']:<11} {len(got):>5} months  ({s['source']} {s['id']})")
            except Exception as e:
                print(f"::warning::{s['key']}: {e}")
                got = {}
            if got or old:
                new_series[s["key"]] = {**meta, "full_history": True, "data": merge(old.get("data"), got, 4)}
            continue
        if s["source"] == "eia" and not eia_key:
            if old:            # keep what was fetched earlier
                new_series[s["key"]] = old
            continue
        data = old.get("data") or {}
        if s["source"] == "yahoo" and not args.full and up_to_date(data, settled):
            print(f"{s['key']:<11} up to date ({max(data)}), not requested")
            new_series[s["key"]] = {**old, **meta}
            continue
        start = start_for(data, old.get("full_history", False) or s["source"] == "yahoo")
        try:
            if s["source"] == "yahoo":
                got = fetch_yahoo(s["id"], start, today)
            else:
                got = fetch_eia(s["route"], s["id"], start, eia_key)
            print(f"{s['key']:<11} {len(got):>5} days from {start}  ({s['source']} {s['id']})")
        except Exception as e:
            print(f"::warning::{s['key']}: {e}")
            failures.append(s["key"])
            got = {}
            fb = EIA_FILL.get(s["key"])
            if fb and eia_key and data:
                after = dt.date.fromisoformat(max(data)) + dt.timedelta(days=1)
                try:
                    got = fetch_eia(fb["route"], fb["id"], after, eia_key)
                    print(f"{s['key']:<11} {len(got):>5} days from {after}  (EIA fallback {fb['id']})")
                except Exception as e2:
                    print(f"::warning::{s['key']} EIA fallback: {e2}")
        full = old.get("full_history", False) or (bool(got) and start == HISTORY_START)
        new_series[s["key"]] = {**meta, "full_history": full, "data": merge(data, got, 3)}

    # exchange rates: ECB first (works from GitHub), Yahoo only if the ECB fails
    old_fx = store.get("fx", {})
    fx_start = min((dt.date.fromisoformat(max(old_fx[f["key"]])) for f in FX if old_fx.get(f["key"])),
                   default=HISTORY_START) - dt.timedelta(days=REFRESH_DAYS)
    new_fx, fx_full = {f["key"]: dict(old_fx.get(f["key"], {})) for f in FX}, set(store.get("fx_full_history", []))
    try:
        got_fx = fetch_ecb_fx(fx_start)
        print("FX          " + ", ".join(f"{k} {len(v)} days" for k, v in got_fx.items()) + f" from {fx_start}  (ECB)")
    except Exception as e:
        print(f"::warning::FX (ECB): {e} - trying Yahoo")
        got_fx = {}
        for f in FX:
            if up_to_date(new_fx[f["key"]], settled):
                continue
            try:
                got_fx[f["key"]] = fetch_yahoo(f["id"], max(fx_start, HISTORY_START), today)
            except Exception as e2:
                print(f"::warning::{f['key']}: {e2}")
    for f in FX:
        new_fx[f["key"]] = merge(new_fx[f["key"]], got_fx.get(f["key"], {}), 5)

    out = {
        "updated": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "series": new_series,
        "fx": new_fx,
        "fx_full_history": sorted(fx_full),
    }
    yahoo_failed = [k for k in failures if any(x["key"] == k and x["source"] == "yahoo" for x in SERIES)]
    if yahoo_failed:
        print(f"::warning::Yahoo refused: {', '.join(yahoo_failed)}. Stored prices are kept for these.")
    if all(store.get(k) == out[k] for k in ("series", "fx", "fx_full_history")):
        print("No new prices - file left unchanged.")
        return not yahoo_failed
    OUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    OUT_FILE.write_text(json.dumps(out, separators=(",", ":")))
    print(f"Wrote {OUT_FILE} ({OUT_FILE.stat().st_size / 1e6:.2f} MB)")
    return not yahoo_failed


if __name__ == "__main__":
    main()
