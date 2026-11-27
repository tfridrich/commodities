"""
Daily update of copper, aluminium, iron ore, lithium carbonate, gold and silver prices for the
metals page (docs/metals/).

Sources (no API keys needed; both answer requests from GitHub Actions, Yahoo Finance does not)
  - Sina Finance global futures API: daily closes of the continuous contracts
        Copper     HG      US cents/lb  COMEX High Grade Copper (stored as USD/lb)
        Iron ore   FEF     USD/t        SGX iron ore 62% Fe CFR China
  - Sina Finance futures API: daily settlement prices of the Chinese exchanges
        Copper     CU0     CNY/t     SHFE copper, main (most active) contract
        Aluminium  AL0     CNY/t     SHFE primary aluminium, main contract
        Gold       AU0     CNY/g     SHFE gold, main contract (no VAT: gold traded on exchange is VAT-exempt)
        Silver     AG0     CNY/kg    SHFE silver, main contract, incl. 13% VAT
        COMEX      GC, SI  USD/oz    COMEX gold and silver, continuous nearby futures (global feed)
        Steel      RB0     CNY/t     SHFE steel rebar, main contract, incl. 13% VAT
                   HC0     CNY/t     SHFE hot-rolled coil, main contract, incl. 13% VAT
        Iron ore   I0      CNY/t     DCE iron ore 62% Fe fines, main contract
        Lithium    LC0     CNY/t     GFEX battery-grade lithium carbonate, main contract
                   LI0     CNY/t     GFEX battery-grade lithium hydroxide, main contract (since 26 Nov 2026)
    and their forward curves from the individual contract months (CU2701, AL2701, I2705, LC2707, ...).
    Chinese futures prices include 13% VAT; the page can show them with or without it.
  - IMF Primary Commodity Prices via FRED (CSV, no key): monthly averages of the LME prices
        PCOPPUSDM  copper, grade A cathode, LME spot, USD/t
        PALUMUSDM  aluminium, 99.5% minimum purity, LME spot, USD/t
    Daily LME prices are licensed and may not be republished; these public monthly averages may.
    They are published with a lag of one to two months.
  - ECB euro reference rates (USD and CNY per EUR) for the unit conversions.

To add a benchmark, add one entry to SERIES (and CURVES for a forward curve) and, if it is
a new unit, one line to UNIT_INFO on the page.

Usage:
    python scripts/update_metals.py                # refresh the last REFRESH_DAYS days
    python scripts/update_metals.py --full         # rebuild the full history
    python scripts/update_metals.py --no-curves    # prices only
    python scripts/update_metals.py --curves-only  # forward curves only

Only the Python standard library is used. The retry/block logic and the curve helpers are
shared with scripts/update_oil_gas.py.
"""
import argparse
import datetime as dt
import json
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent))
import update_oil_gas as og  # noqa: E402  (Yahoo client, retry/block logic, curve helpers)

OUT_FILE = Path(__file__).resolve().parent.parent / "docs" / "data" / "metals.json"
CURVE_FILE = OUT_FILE.parent / "metals_curves.json"
HISTORY_START = dt.date(2000, 1, 1)
REFRESH_DAYS = 45
CN_TZ = ZoneInfo("Asia/Shanghai")
CN_SETTLED_HOUR = 16      # Chinese day session closes 15:00; a bar for today counts as final after 16:00
CN_VAT = 0.13

SERIES = [
    # key, group, label, unit, source, id, vat, description
    dict(key="comex", group="copper", label="COMEX copper", unit="USD/lb", source="sina_global", id="HG",
         scale=0.01, tz="America/New_York", settled_hour=17,
         desc="COMEX High Grade Copper, continuous nearby futures, daily close"),
    dict(key="shfe", group="copper", label="SHFE copper", unit="CNY/t", source="sina", id="CU0", vat=CN_VAT,
         desc="Shanghai Futures Exchange copper cathode, main contract, settlement price incl. 13% VAT"),
    dict(key="lme_cu", group="copper", label="LME copper (IMF monthly)", unit="USD/t", source="fred", id="PCOPPUSDM",
         freq="monthly",
         desc="IMF global price of copper: monthly average of the LME grade A cathode spot price, published one to two months later"),
    dict(key="shfe_al", group="aluminium", label="SHFE aluminium", unit="CNY/t", source="sina", id="AL0", vat=CN_VAT,
         desc="Shanghai Futures Exchange primary aluminium (Al 99.70%), main contract, settlement price incl. 13% VAT"),
    dict(key="lme_al", group="aluminium", label="LME aluminium (IMF monthly)", unit="USD/t", source="fred", id="PALUMUSDM",
         freq="monthly",
         desc="IMF global price of aluminium: monthly average of the LME 99.5% minimum purity spot price, published one to two months later"),
    dict(key="comex_au", group="gold", label="COMEX gold", unit="USD/oz", source="sina_global", id="GC",
         tz="America/New_York", settled_hour=17, expect=(500, 20000),
         desc="COMEX gold, continuous nearby futures, daily close, USD per troy ounce"),
    dict(key="shfe_au", group="gold", label="SHFE gold", unit="CNY/g", source="sina", id="AU0", expect=(100, 5000),
         desc="Shanghai Futures Exchange gold (Au 99.95%), main contract, settlement price, CNY per gram; no VAT"),
    dict(key="comex_ag", group="silver", label="COMEX silver", unit="USD/oz", source="sina_global", id="SI",
         tz="America/New_York", settled_hour=17, expect=(3, 500),
         desc="COMEX silver, continuous nearby futures, daily close, USD per troy ounce"),
    dict(key="shfe_ag", group="silver", label="SHFE silver", unit="CNY/kg", source="sina", id="AG0", vat=CN_VAT,
         expect=(1000, 200000),
         desc="Shanghai Futures Exchange silver (Ag 99.99%), main contract, settlement price incl. 13% VAT, CNY per kg"),
    dict(key="shfe_rb", group="steel", label="SHFE rebar", unit="CNY/t", source="sina", id="RB0", vat=CN_VAT,
         expect=(1000, 12000),
         desc="Shanghai Futures Exchange steel rebar (HRB400E), main contract, settlement price incl. 13% VAT"),
    dict(key="shfe_hc", group="steel", label="SHFE hot-rolled coil", unit="CNY/t", source="sina", id="HC0", vat=CN_VAT,
         expect=(1000, 12000),
         desc="Shanghai Futures Exchange hot-rolled steel coil (Q235B), main contract, settlement price incl. 13% VAT"),
    dict(key="sgx", group="iron", label="SGX iron ore 62%", unit="USD/t", source="sina_global", id="FEF",
         tz="Asia/Singapore", settled_hour=20,
         desc="SGX iron ore 62% Fe CFR China futures, continuous nearby contract, daily close, USD per dry tonne"),
    dict(key="dce", group="iron", label="DCE iron ore", unit="CNY/t", source="sina", id="I0", vat=CN_VAT,
         desc="Dalian Commodity Exchange iron ore 62% Fe fines, main contract, settlement price per wet tonne at port, incl. 13% VAT"),
    dict(key="gfex", group="lithium", label="GFEX lithium carbonate", unit="CNY/t", source="sina", id="LC0", vat=CN_VAT,
         desc="Guangzhou Futures Exchange battery-grade lithium carbonate (99.5% Li2CO3), main contract, settlement price incl. 13% VAT"),
    # listed 26 Nov 2026; the first contracts deliver June-November 2027
    dict(key="gfex_li", group="lithium", label="GFEX lithium hydroxide", unit="CNY/t", source="sina", id="LI0", vat=CN_VAT,
         expect=(10000, 1000000),
         desc="Guangzhou Futures Exchange battery-grade lithium hydroxide, main contract, settlement price incl. 13% VAT"),
    # Coal is shown on the Fuels page (docs/oil-gas/), which reads group "coal" from metals.json
    dict(key="coal_au", group="coal", label="Thermal coal Australia (IMF monthly)", unit="USD/t", source="fred",
         id="PCOALAUUSDM", freq="monthly", expect=(20, 1000),
         desc="IMF global price of thermal coal, Australia: monthly average of the Newcastle export price (6,000 kcal/kg, FOB), published one to two months later"),
    dict(key="dce_jm", group="coal", label="DCE coking coal", unit="CNY/t", source="sina", id="JM0", vat=CN_VAT,
         expect=(300, 6000),
         desc="Dalian Commodity Exchange coking coal (metallurgical coal), main contract, settlement price incl. 13% VAT"),
    dict(key="dce_j", group="coal", label="DCE coke", unit="CNY/t", source="sina", id="J0", vat=CN_VAT,
         expect=(500, 8000),
         desc="Dalian Commodity Exchange metallurgical coke, main contract, settlement price incl. 13% VAT"),
]
# FX stored as USD per unit of the currency (the page expects that), from ECB rates per EUR
FX_KEYS = ("EURUSD", "CNYUSD")
FX_START = dt.date(1999, 1, 4)

# Forward curves. Sina: root + yymm (LC2707). (A Yahoo entry would be root + month code + yy +
# suffix, e.g. HGZ26.CMX, but Yahoo refuses requests from GitHub Actions, so none is configured.)
# `monthly` consecutive contract months are requested from `back_months` before today, plus
# December contracts for `dec_years` years ahead. Months that are not listed are skipped.
CURVES = [
    dict(key="shfe", group="copper", label="SHFE copper", unit="CNY/t", source="sina", root="CU", vat=CN_VAT,
         monthly=13, dec_years=0, desc="SHFE copper futures (CU), settlement prices incl. 13% VAT"),
    dict(key="shfe_al", group="aluminium", label="SHFE aluminium", unit="CNY/t", source="sina", root="AL", vat=CN_VAT,
         monthly=13, dec_years=0, desc="SHFE aluminium futures (AL), settlement prices incl. 13% VAT"),
    dict(key="shfe_au", group="gold", label="SHFE gold", unit="CNY/g", source="sina", root="AU", expect=(100, 5000),
         monthly=13, dec_years=0, desc="SHFE gold futures (AU), settlement prices, CNY per gram; no VAT"),
    dict(key="shfe_ag", group="silver", label="SHFE silver", unit="CNY/kg", source="sina", root="AG", vat=CN_VAT,
         expect=(1000, 200000), monthly=13, dec_years=0,
         desc="SHFE silver futures (AG), settlement prices incl. 13% VAT, CNY per kg"),
    dict(key="shfe_rb", group="steel", label="SHFE rebar", unit="CNY/t", source="sina", root="RB", vat=CN_VAT,
         expect=(1000, 12000), monthly=13, dec_years=0, desc="SHFE steel rebar futures (RB), settlement prices incl. 13% VAT"),
    dict(key="shfe_hc", group="steel", label="SHFE hot-rolled coil", unit="CNY/t", source="sina", root="HC", vat=CN_VAT,
         expect=(1000, 12000), monthly=13, dec_years=0, desc="SHFE hot-rolled coil futures (HC), settlement prices incl. 13% VAT"),
    dict(key="dce", group="iron", label="DCE iron ore", unit="CNY/t", source="sina", root="I", vat=CN_VAT,
         monthly=13, dec_years=0, desc="DCE iron ore futures (I), settlement prices incl. 13% VAT"),
    dict(key="gfex", group="lithium", label="GFEX lithium carbonate", unit="CNY/t", source="sina", root="LC", vat=CN_VAT,
         monthly=13, dec_years=0, desc="GFEX lithium carbonate futures (LC), settlement prices incl. 13% VAT"),
    dict(key="gfex_li", group="lithium", label="GFEX lithium hydroxide", unit="CNY/t", source="sina", root="LI", vat=CN_VAT,
         expect=(10000, 1000000), monthly=13, dec_years=0,
         desc="GFEX lithium hydroxide futures (LI), settlement prices incl. 13% VAT"),
    dict(key="dce_jm", group="coal", label="DCE coking coal", unit="CNY/t", source="sina", root="JM", vat=CN_VAT,
         expect=(300, 6000), monthly=13, dec_years=0, desc="DCE coking coal futures (JM), settlement prices incl. 13% VAT"),
    dict(key="dce_j", group="coal", label="DCE coke", unit="CNY/t", source="sina", root="J", vat=CN_VAT,
         expect=(500, 8000), monthly=13, dec_years=0, desc="DCE coke futures (J), settlement prices incl. 13% VAT"),
]
CURVE_BACKFILL_DAYS = 400
CURVE_REFRESH_DAYS = 10

SINA_URL = ("https://stock2.finance.sina.com.cn/futures/api/jsonp.php/var%20_x=/"
            "InnerFuturesNewService.getDailyKLine?symbol={symbol}")
SINA_GLOBAL_URL = ("https://stock2.finance.sina.com.cn/futures/api/jsonp.php/var%20_x=/"
                   "GlobalFuturesService.getGlobalFuturesDailyKLine?symbol={symbol}&source=web")
SINA_HOST = "sina.com.cn"     # keys for the consecutive-failure counter
ECB_URL = ("https://data-api.ecb.europa.eu/service/data/EXR/D.USD+CNY.EUR.SP00.A"
           "?format=csvdata&detail=dataonly&startPeriod={start}")
ECB_HOST = "ecb.europa.eu"
FRED_URL = "https://fred.stlouisfed.org/graph/fredgraph.csv?id={id}"
FRED_HOST = "stlouisfed.org"


# ---------------------------------------------------------------- Sina Finance

def get_text(url, tries=2, host=SINA_HOST):
    """GET a text document with the same retry and 'source is blocking' rules as og.get_json."""
    if og.FAILS_IN_ROW.get(host, 0) >= og.MAX_FAILS_IN_ROW:
        raise og.SourceBlocked(f"{host} is refusing requests in this run - skipped")
    last = None
    for i in range(tries):
        try:
            headers = {"User-Agent": og.UA}
            if host == SINA_HOST:
                headers["Referer"] = "https://finance.sina.com.cn/"
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=30) as r:
                body = r.read().decode("utf-8", errors="replace")
            og.FAILS_IN_ROW[host] = 0
            return body
        except urllib.error.HTTPError as e:
            last = e
            if e.code in (400, 401, 403, 404):
                break
        except (urllib.error.URLError, TimeoutError) as e:
            last = e
        if i < tries - 1:
            time.sleep(5)
    og.FAILS_IN_ROW[host] = og.FAILS_IN_ROW.get(host, 0) + 1
    if og.FAILS_IN_ROW[host] >= og.MAX_FAILS_IN_ROW:
        raise og.SourceBlocked(f"{host} refused {og.MAX_FAILS_IN_ROW} requests in a row (last error: {last})")
    raise last


class UnexpectedResponse(ValueError):
    """Sina answered, but not with its JSONP format (e.g. a block or captcha page)."""


def _num(x):
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if v > 0 else None


def parse_sina(text, now_utc=None, tz=CN_TZ, settled_hour=CN_SETTLED_HOUR, scale=1.0):
    """Sina JSONP -> {date: price}: the settlement price, else the close (the global futures
    feed has no settlement). Contracts that are not listed return `null`. A bar for today
    (in the exchange's time zone) is only kept after `settled_hour`, and bars dated in the
    future (evening sessions are booked to the next trading day) are dropped."""
    m = re.search(r"var _x=\((\[.*\]|null)\)", text, re.S)
    if not m:
        raise UnexpectedResponse("Sina returned an unexpected page: " + text[:80].replace("\n", " "))
    if m.group(1) == "null":
        return {}
    now_ex = (now_utc or dt.datetime.now(dt.timezone.utc)).astimezone(tz)
    out = {}
    for bar in json.loads(m.group(1)):
        d = bar.get("d") or bar.get("date")
        v = (_num(bar.get("s")) or _num(bar.get("settlement"))
             or _num(bar.get("c")) or _num(bar.get("close")))
        if not d or v is None:
            continue
        day = dt.date.fromisoformat(d)
        if day > now_ex.date() or (day == now_ex.date() and now_ex.hour < settled_hour):
            continue
        out[d] = round(v * scale, 6)
    return out


def plausible(spec, got, what):
    """Drop a download whose typical price is outside the series' expected range (wrong symbol
    or unit), so it never reaches the page. Returns `got` or {}."""
    rng = spec.get("expect")
    if not rng or not got:
        return got
    vals = sorted(got.values())
    med = vals[len(vals) // 2]
    if rng[0] <= med <= rng[1]:
        return got
    print(f"::warning::{what}: typical price {med:g} is outside the expected {rng[0]:g}-{rng[1]:g} {spec['unit']} - not used")
    return {}


def fetch_sina(symbol, start, pause=0.5, spec=None):
    """Chinese futures by default; with `spec` (a SERIES entry with source "sina_global"),
    the global futures feed in that exchange's time zone."""
    time.sleep(pause)
    url, kw = SINA_URL, {}
    if spec:
        url = SINA_GLOBAL_URL
        kw = dict(tz=ZoneInfo(spec["tz"]), settled_hour=spec["settled_hour"], scale=spec.get("scale", 1.0))
    try:
        got = parse_sina(get_text(url.format(symbol=urllib.parse.quote(symbol))), **kw)
    except UnexpectedResponse:
        # count it like a refused request, so a blocked source stops the run quickly
        og.FAILS_IN_ROW[SINA_HOST] = og.FAILS_IN_ROW.get(SINA_HOST, 0) + 1
        if og.FAILS_IN_ROW[SINA_HOST] >= og.MAX_FAILS_IN_ROW:
            raise og.SourceBlocked(f"{SINA_HOST} returned {og.MAX_FAILS_IN_ROW} unexpected pages in a row")
        raise
    return {d: v for d, v in got.items() if d >= start.isoformat()}


# ---------------------------------------------------------------- IMF monthly prices via FRED

def parse_fred(text, series_id):
    """FRED CSV (observation_date,ID) -> {"YYYY-MM-01": value}. Missing values are '.' or empty."""
    lines = text.strip().splitlines()
    if not lines or "," not in lines[0] or series_id not in lines[0]:
        raise ValueError("FRED returned an unexpected page: " + text[:80].replace("\n", " "))
    out = {}
    for line in lines[1:]:
        d, _, v = line.partition(",")
        v = _num(v.strip())
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", d.strip()) and v is not None:
            out[d.strip()] = round(v, 4)
    return out


def fetch_fred(series_id):
    """The whole series (a few hundred months, one small request)."""
    return parse_fred(get_text(FRED_URL.format(id=series_id), host=FRED_HOST), series_id)


# ---------------------------------------------------------------- ECB exchange rates

def parse_ecb(text):
    """ECB CSV (USD and CNY per EUR) -> {"EURUSD": {date: USD per EUR}, "CNYUSD": {date: USD per CNY}}."""
    lines = text.strip().splitlines()
    if not lines or not lines[0].startswith("KEY,"):
        raise UnexpectedResponse("ECB returned an unexpected page: " + text[:80].replace("\n", " "))
    head = lines[0].split(",")
    i_ccy, i_day, i_val = head.index("CURRENCY"), head.index("TIME_PERIOD"), head.index("OBS_VALUE")
    per_eur = {"USD": {}, "CNY": {}}
    for line in lines[1:]:
        f = line.split(",")
        v = _num(f[i_val]) if len(f) > i_val else None
        if v is not None and f[i_ccy] in per_eur:
            per_eur[f[i_ccy]][f[i_day]] = v
    usd = per_eur["USD"]
    return {"EURUSD": {d: round(v, 6) for d, v in usd.items()},
            "CNYUSD": {d: round(usd[d] / v, 6) for d, v in per_eur["CNY"].items() if d in usd}}


def fetch_ecb(start):
    return parse_ecb(get_text(ECB_URL.format(start=start.isoformat()), host=ECB_HOST))


# ---------------------------------------------------------------- prices

def update_prices(args, today):
    store = json.loads(OUT_FILE.read_text()) if OUT_FILE.exists() else {"series": {}, "fx": {}}
    failures = []

    def start_for(existing, full_done=True):
        if args.full or not existing or not full_done:
            return HISTORY_START
        return max(HISTORY_START, dt.date.fromisoformat(max(existing)) - dt.timedelta(days=REFRESH_DAYS))

    new_series = {}
    for s in SERIES:
        old = store.get("series", {}).get(s["key"], {})
        meta = {k: s[k] for k in ("group", "label", "unit", "source", "id", "desc")}
        for k in ("vat", "freq"):
            if s.get(k):
                meta[k] = s[k]
        start = start_for(old.get("data"), old.get("full_history", False))
        try:
            if s["source"] == "yahoo":
                got = og.fetch_yahoo(s["id"], start, today)
            elif s["source"] == "fred":
                got = fetch_fred(s["id"])
            elif s["source"] == "sina_global":
                got = fetch_sina(s["id"], start, spec=s)
            else:
                got = fetch_sina(s["id"], start)
            got = plausible(s, got, s["key"])
            what = f"{len(got):>5} months" if s.get("freq") == "monthly" else f"{len(got):>5} days from {start}"
            print(f"{s['key']:<8} {what}  ({s['source']} {s['id']})")
        except Exception as e:
            print(f"::warning::{s['key']}: {e}")
            failures.append(s["key"])
            got = {}
        full = old.get("full_history", False) or (bool(got) and (start == HISTORY_START or s["source"] == "fred"))
        new_series[s["key"]] = {**meta, "full_history": full, "data": og.merge(old.get("data"), got, 4)}

    # one ECB request covers both rates; the full history is fetched until it has succeeded once
    old_fx = store.get("fx", {})
    fx_full = set(store.get("fx_full_history", []))
    fx_done = all(k in fx_full for k in FX_KEYS)
    fx_start = FX_START if args.full or not fx_done or not all(old_fx.get(k) for k in FX_KEYS) else \
        dt.date.fromisoformat(min(max(old_fx[k]) for k in FX_KEYS)) - dt.timedelta(days=REFRESH_DAYS)
    try:
        got_fx = fetch_ecb(fx_start)
        print("FX       " + ", ".join(f"{k} {len(v)} days" for k, v in got_fx.items()) + f" from {fx_start}  (ECB)")
    except Exception as e:
        print(f"::warning::FX (ECB): {e}")
        failures.extend(FX_KEYS)
        got_fx = {}
    new_fx = {}
    for k in FX_KEYS:
        got = got_fx.get(k, {})
        if got and fx_start == FX_START:
            fx_full.add(k)
        new_fx[k] = og.merge(old_fx.get(k), got, 6)

    out = {"updated": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
           "series": new_series, "fx": new_fx, "fx_full_history": sorted(fx_full)}
    if len(failures) == len(SERIES) + len(FX_KEYS):
        print("::error::Prices not updated: every request failed. The stored prices are kept.")
        return False
    if all(store.get(k) == out[k] for k in ("series", "fx", "fx_full_history")):
        print("No new prices - file left unchanged.")
        return True
    OUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    OUT_FILE.write_text(json.dumps(out, separators=(",", ":")))
    print(f"Wrote {OUT_FILE} ({OUT_FILE.stat().st_size / 1e6:.2f} MB)")
    return True


# ---------------------------------------------------------------- forward curves

def curve_symbol(c, ym):
    y, m = int(ym[:4]), int(ym[5:])
    if c["source"] == "yahoo":
        return f"{c['root']}{og.MONTH_CODES[m - 1]}{y % 100:02d}{c['suffix']}"
    return f"{c['root']}{y % 100:02d}{m:02d}"


def build_snapshots(histories, source):
    """{delivery month: {date: price}} -> {date: {delivery month: price}}.
    COMEX copper trades until three business days before the end of the delivery month, but
    the spot month is thin and Yahoo can keep repeating a last price after expiry, so for
    Yahoo the curve starts with the next month (as on the oil-gas page). Chinese contracts
    trade until about the 15th of the delivery month and Sina's history of a contract ends at
    its expiry, so all its bars are kept. Days with fewer than half the usual number of
    contracts are dropped (holidays, partial data)."""
    by_date = {}
    for ym, hist in histories.items():
        for d, v in hist.items():
            if source == "yahoo" and d[:7] >= ym:
                continue
            by_date.setdefault(d, {})[ym] = round(v, 4)
    if not by_date:
        return {}
    sizes = sorted(len(c) for c in by_date.values())
    median = sizes[len(sizes) // 2]
    return {d: dict(sorted(c.items())) for d, c in by_date.items() if len(c) >= 0.5 * median}


def update_curves(today, full=False):
    store = json.loads(CURVE_FILE.read_text()) if CURVE_FILE.exists() else {"curves": {}}
    out, changed, blocked = {"curves": {}}, False, set()
    for c in CURVES:
        old = store.get("curves", {}).get(c["key"], {})
        snaps = old.get("snapshots", {})
        meta = {k: c[k] for k in ("group", "label", "unit", "source", "root", "desc")}
        if c.get("vat"):
            meta["vat"] = c["vat"]
        if c["source"] in blocked:
            out["curves"][c["key"]] = old or {**meta, "snapshots": {}}
            continue
        backfill = full or not snaps
        days = CURVE_BACKFILL_DAYS if backfill else CURVE_REFRESH_DAYS
        start = today - dt.timedelta(days=days)
        # on a backfill, also request the contracts that expired during the backfill window
        back = days // 30 + 1 if backfill else 1
        months = og.contract_months(today, c["monthly"], c["dec_years"], back_months=back)
        histories = {}
        for ym in months:
            sym = curve_symbol(c, ym)
            try:
                if c["source"] == "yahoo":
                    got = og.fetch_yahoo(sym, start, today, missing_ok=True, pause=0.3)
                else:
                    got = fetch_sina(sym, start, pause=0.3)
                got = plausible(c, got, f"{c['key']} {ym}")
            except og.SourceBlocked as e:
                print(f"::error::{c['key']} forward curve not updated: {e}. The stored curve is kept.")
                blocked.add(c["source"])
                histories = None
                break
            except Exception as e:
                print(f"::warning::{c['key']} {ym}: {e}")
                got = {}
            if got:
                histories[ym] = got
        if histories is None:
            out["curves"][c["key"]] = old or {**meta, "snapshots": {}}
            continue
        new = build_snapshots(histories, c["source"])
        print(f"curve {c['key']:<6} {len(histories):>3} contracts, {len(new):>3} days from {start}")
        merged = {d: dict(v) for d, v in snaps.items()}
        for d, curve in new.items():
            merged[d] = dict(sorted({**merged.get(d, {}), **curve}.items()))
        merged = og.prune_snapshots(merged)
        out["curves"][c["key"]] = {**meta, "snapshots": dict(sorted(merged.items()))}
        changed |= merged != snaps or {k: old.get(k) for k in meta} != meta
    if not changed:
        print("No new forward curves - file left unchanged.")
        return len(blocked) < len({c["source"] for c in CURVES})
    out["updated"] = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    CURVE_FILE.write_text(json.dumps(out, separators=(",", ":")))
    print(f"Wrote {CURVE_FILE} ({CURVE_FILE.stat().st_size / 1e6:.2f} MB)")
    return True


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--full", action="store_true", help="re-download the whole history")
    ap.add_argument("--no-curves", action="store_true", help="skip the forward curves")
    ap.add_argument("--curves-only", action="store_true", help="only update the forward curves")
    args = ap.parse_args()
    sys.stdout.reconfigure(line_buffering=True)
    today = dt.datetime.now(dt.timezone.utc).date()
    prices_ok = True if args.curves_only else update_prices(args, today)
    curves_ok = True if args.no_curves else update_curves(today, full=args.full)
    if not prices_ok and not curves_ok:
        sys.exit("Nothing could be fetched - see the messages above.")


if __name__ == "__main__":
    main()
