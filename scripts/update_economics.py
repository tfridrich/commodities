"""
Daily update of exchange rates and of the monthly and quarterly price and cost indices shown on
the Economics page (docs/economics/) and on the Steel and Cement & concrete tabs of the metals page.

Sources (all free, no key, open licences):
  - ECB euro foreign exchange reference rates: USD, CNY and GBP per EUR, daily since 1999.
  - FRED (Federal Reserve Bank of St. Louis), US producer price indices of the Bureau of Labor
    Statistics (public domain), CSV:
        WPU1017          steel mill products
        WPU10170410      hot-rolled steel bars, plates and structural shapes, carbon
        WPU1322          cement, hydraulic
        PCU327320327320  ready-mix concrete manufacturing
  - Eurostat (CC BY 4.0), JSON-stat API, Germany:
        sts_inppd_m  domestic producer prices, 2021=100: C24 basic metals, C2351 cement,
                     C236 articles of concrete, cement and plaster
        sts_copi_q   new residential buildings: construction prices (PRC_PRR) and costs (COST), 2021=100
        lc_lci_r2_q  labour cost index, construction (F) and whole economy (B-S), 2020=100
        prc_hicp_minr  consumer price inflation (HICP, annual rate): Germany, Austria, euro area
  - Inflation outside the EU: US CPI-U (BLS, FRED CPIAUCNS, annual rate calculated here) and UK CPI
    annual rate (Office for National Statistics, series D7G7, JSON).

Monthly values are stored under the first day of the month, quarterly values under the first day
of the quarter. The pages rebase every index to its 2021 average = 100, so all lines start from
the same point whatever base year the publisher uses.

    python scripts/update_economics.py
Writes docs/data/economics.json. Only the Python standard library is used.
"""
import datetime as dt
import json
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

try:                      # use the operating system's certificates when available (Windows/Anaconda)
    import truststore
    truststore.inject_into_ssl()
except Exception:
    pass

ROOT = Path(__file__).resolve().parent.parent
OUT_FILE = ROOT / "docs" / "data" / "economics.json"
UA = "commodities-site-updater (github.com/tfridrich/commodities)"

FX_START = dt.date(1999, 1, 4)
FX_REFRESH_DAYS = 10
FX_CCY = ["USD", "CNY", "GBP"]
ECB_URL = ("https://data-api.ecb.europa.eu/service/data/EXR/D.{ccy}.EUR.SP00.A"
           "?format=csvdata&detail=dataonly&startPeriod={start}")
FRED_URL = "https://fred.stlouisfed.org/graph/fredgraph.csv?id={id}"
FRED_FROM = "2000-01-01"
EUROSTAT_URL = "https://ec.europa.eu/eurostat/api/dissemination/statistics/1.0/data/{ds}?"

EUROSTAT_DE = "https://ec.europa.eu/eurostat/databrowser/view/{ds}/default/table"
INDICES = [
    # ------------------------------------------------------------- steel (metals page)
    dict(key="us_steel_mill", group="steel", country="US", label="US steel mill products",
         source="fred", id="WPU1017", freq="M",
         desc="US producer price index for steel mill products (BLS, via FRED)"),
    dict(key="us_steel_hr", group="steel", country="US", label="US hot-rolled bars, plates and shapes",
         source="fred", id="WPU10170410", freq="M",
         desc="US producer price index for hot-rolled carbon steel bars, plates and structural shapes (BLS, via FRED)"),
    dict(key="de_basic_metals", group="steel", country="DE", label="Germany basic metals",
         source="eurostat", ds="sts_inppd_m", freq="M",
         filters=dict(geo="DE", indic_bt="PRC_PRR_DOM", nace_r2="C24", unit="I21", s_adj="NSA"),
         desc="German producer price index for basic metals (iron and steel plus aluminium, copper and other metals), domestic sales (Eurostat)"),
    # ------------------------------------------------------------- cement and concrete (metals page)
    dict(key="us_cement", group="cement", country="US", label="US cement",
         source="fred", id="WPU1322", freq="M",
         desc="US producer price index for hydraulic cement (BLS, via FRED)"),
    dict(key="us_readymix", group="cement", country="US", label="US ready-mix concrete",
         source="fred", id="PCU327320327320", freq="M",
         desc="US producer price index for ready-mix concrete manufacturing (BLS, via FRED)"),
    dict(key="de_cement", group="cement", country="DE", label="Germany cement",
         source="eurostat", ds="sts_inppd_m", freq="M",
         filters=dict(geo="DE", indic_bt="PRC_PRR_DOM", nace_r2="C2351", unit="I21", s_adj="NSA"),
         desc="German producer price index for cement, domestic sales (Eurostat)"),
    dict(key="de_concrete", group="cement", country="DE", label="Germany concrete products",
         source="eurostat", ds="sts_inppd_m", freq="M",
         filters=dict(geo="DE", indic_bt="PRC_PRR_DOM", nace_r2="C236", unit="I21", s_adj="NSA"),
         desc="German producer price index for articles of concrete, cement and plaster (ready-mixed concrete, precast parts, blocks), domestic sales (Eurostat)"),
    # ------------------------------------------------------------- construction (economics page)
    dict(key="de_constr_price", group="construction", country="DE", label="Germany construction prices",
         source="eurostat", ds="sts_copi_q", freq="Q",
         filters=dict(geo="DE", indic_bt="PRC_PRR", cpa2_1="CPA_F41001_X_410014", unit="I21", s_adj="NSA"),
         desc="Prices builders charge for new residential buildings, incl. VAT (Destatis construction price index, via Eurostat)"),
    dict(key="de_constr_cost", group="construction", country="DE", label="Germany construction costs",
         source="eurostat", ds="sts_copi_q", freq="Q",
         filters=dict(geo="DE", indic_bt="COST", cpa2_1="CPA_F41001_X_410014", unit="I21", s_adj="NSA"),
         desc="Costs of materials and labour for building new residential buildings (Destatis construction cost index, via Eurostat)"),
    # ------------------------------------------------------------- labour (economics page)
    dict(key="de_lci_constr", group="labour", country="DE", label="Germany labour costs, construction",
         source="eurostat", ds="lc_lci_r2_q", freq="Q",
         filters=dict(geo="DE", lcstruct="D1_D4_MD5", nace_r2="F", unit="I20"), s_adj=["SCA", "NSA"],
         desc="Labour cost index per hour worked, construction sector: wages plus employer social contributions and taxes (Eurostat)"),
    dict(key="de_lci_all", group="labour", country="DE", label="Germany labour costs, whole economy",
         source="eurostat", ds="lc_lci_r2_q", freq="Q",
         filters=dict(geo="DE", lcstruct="D1_D4_MD5", nace_r2="B-S", unit="I20"), s_adj=["SCA", "NSA"],
         desc="Labour cost index per hour worked, industry, construction and services (Eurostat)"),
    dict(key="de_lci_constr_wages", group="labour", country="DE", label="Germany wages, construction",
         source="eurostat", ds="lc_lci_r2_q", freq="Q",
         filters=dict(geo="DE", lcstruct="D11", nace_r2="F", unit="I20"), s_adj=["SCA", "NSA"], off=True,
         desc="Wages and salaries per hour worked, construction sector (Eurostat)"),
]
PLAUSIBLE = (5, 2000)        # any index outside this range is a wrong series or unit
PLAUSIBLE_RATE = (-20, 60)   # annual inflation rate in %

# Annual consumer price inflation in %, monthly (stored in "rates")
RATES = [
    dict(key="infl_ea", group="inflation", country="EA", label="Euro area", source="eurostat", ds="prc_hicp_minr", freq="M",
         filters=dict(geo="EA20", coicop18="TOTAL", unit="RCH_A"),
         desc="Harmonised index of consumer prices (HICP), euro area, annual rate (Eurostat)"),
    dict(key="infl_de", group="inflation", country="DE", label="Germany", source="eurostat", ds="prc_hicp_minr", freq="M",
         filters=dict(geo="DE", coicop18="TOTAL", unit="RCH_A"),
         desc="Harmonised index of consumer prices (HICP), Germany, annual rate (Eurostat)"),
    dict(key="infl_at", group="inflation", country="AT", label="Austria", source="eurostat", ds="prc_hicp_minr", freq="M",
         filters=dict(geo="AT", coicop18="TOTAL", unit="RCH_A"),
         desc="Harmonised index of consumer prices (HICP), Austria, annual rate (Eurostat)"),
    dict(key="infl_us", group="inflation", country="US", label="United States", source="fred_yoy", id="CPIAUCNS", freq="M",
         desc="Consumer price index for all urban consumers (CPI-U), annual rate (BLS, via FRED)"),
    dict(key="infl_uk", group="inflation", country="UK", label="United Kingdom", source="ons", id="d7g7", freq="M",
         desc="Consumer prices index (CPI), annual rate (Office for National Statistics, series D7G7)"),
]
ONS_URL = "https://www.ons.gov.uk/economy/inflationandpriceindices/timeseries/{id}/mm23/data"
MONTHS = {m: i for i, m in enumerate(["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"], 1)}


# ---------------------------------------------------------------- HTTP

def get(url, tries=3, timeout=60):
    last = None
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as e:
            last = e
            if e.code in (400, 404):
                raise
        except (urllib.error.URLError, TimeoutError) as e:
            last = e
        time.sleep(3 * (i + 1))
    raise RuntimeError(f"{urllib.parse.urlsplit(url).netloc}: {last}")


# ---------------------------------------------------------------- ECB exchange rates

def parse_ecb_csv(text):
    lines = text.strip().splitlines()
    if not lines or not lines[0].startswith("KEY,"):
        raise ValueError("ECB returned an unexpected page: " + text[:80].replace("\n", " "))
    head = lines[0].split(",")
    i_d, i_v = head.index("TIME_PERIOD"), head.index("OBS_VALUE")
    out = {}
    for line in lines[1:]:
        f = line.split(",")
        try:
            out[f[i_d]] = round(float(f[i_v]), 6)
        except (ValueError, IndexError):
            pass
    return out


def update_fx(old_fx):
    new = {c: dict(old_fx.get(c, {})) for c in FX_CCY}
    for c in FX_CCY:
        have = new[c]
        start = (dt.date.fromisoformat(max(have)) - dt.timedelta(days=FX_REFRESH_DAYS)) if have else FX_START
        try:
            got = parse_ecb_csv(get(ECB_URL.format(ccy=c, start=start.isoformat())))
            have.update(got)
            print(f"FX  EUR/{c}: {len(got):>5} days from {start}  (ECB)")
        except Exception as e:
            print(f"::warning::FX EUR/{c}: {e} - stored rates kept")
        new[c] = dict(sorted(have.items()))
    return new


# ---------------------------------------------------------------- FRED

def parse_fred_csv(text, series_id):
    lines = text.strip().splitlines()
    if not lines or series_id not in lines[0]:
        raise ValueError("FRED returned an unexpected page: " + text[:80].replace("\n", " "))
    out = {}
    for line in lines[1:]:
        d, _, v = line.partition(",")
        d = d.strip()
        if d < FRED_FROM or not re.fullmatch(r"\d{4}-\d{2}-01", d):
            continue
        try:
            out[d] = round(float(v), 4)
        except ValueError:
            pass          # '.' marks a missing month
    return out


def fetch_fred(spec):
    return parse_fred_csv(get(FRED_URL.format(id=spec["id"])), spec["id"])


def fetch_fred_yoy(spec):
    """Annual rate of a monthly price index: value / same month a year earlier - 1, in %."""
    global FRED_FROM
    keep, FRED_FROM = FRED_FROM, "1999-01-01"
    try:
        idx = fetch_fred(spec)
    finally:
        FRED_FROM = keep
    out = {}
    for d, v in idx.items():
        prev = idx.get(f"{int(d[:4]) - 1}{d[4:]}")
        if prev and d >= "2000-01-01":
            out[d] = round((v / prev - 1) * 100, 1)
    return out


def fetch_ons(spec):
    """ONS time series JSON: months = [{"date": "2026 JAN", "value": "3.0", ...}]."""
    js = json.loads(get(ONS_URL.format(id=spec["id"])))
    out = {}
    for m in js.get("months", []):
        mm = re.fullmatch(r"(\d{4}) ([A-Z]{3})", str(m.get("date", "")).strip().upper())
        if not mm or mm.group(2) not in MONTHS:
            continue
        try:
            v = float(m.get("value"))
        except (TypeError, ValueError):
            continue
        d = f"{mm.group(1)}-{MONTHS[mm.group(2)]:02d}-01"
        if d >= "2000-01-01":
            out[d] = round(v, 1)
    return out


# ---------------------------------------------------------------- Eurostat (JSON-stat 2.0)

def period_to_date(p):
    """'2026-10' -> '2026-10-01', '2026-Q2' -> '2026-04-01'."""
    m = re.fullmatch(r"(\d{4})-(\d{2})", p)
    if m:
        return f"{m.group(1)}-{m.group(2)}-01"
    m = re.fullmatch(r"(\d{4})-?Q([1-4])", p)
    if m:
        return f"{m.group(1)}-{(int(m.group(2)) - 1) * 3 + 1:02d}-01"
    raise ValueError(f"unexpected Eurostat period {p!r}")


def parse_jsonstat(js):
    """One series (every dimension but time has a single category) -> {date: value}."""
    if "error" in js:
        raise ValueError(f"Eurostat: {js['error']}")
    ids, sizes = js["id"], js["size"]
    if any(n != 1 for d, n in zip(ids, sizes) if d != "time"):
        raise ValueError("Eurostat returned more than one series: " +
                         ", ".join(f"{d}={n}" for d, n in zip(ids, sizes) if n != 1 and d != "time"))
    t_idx = js["dimension"]["time"]["category"]["index"]
    pos_to_period = {pos: p for p, pos in t_idx.items()}
    # with all other dimensions of size 1, the flat value index equals the time position
    out = {}
    for k, v in js.get("value", {}).items():
        if v is None:
            continue
        out[period_to_date(pos_to_period[int(k)])] = round(float(v), 4)
    return out


def fetch_eurostat(spec):
    options = spec.get("s_adj") or [None]
    last = None
    for s_adj in options:
        params = dict(spec["filters"], format="JSON", lang="EN")
        if s_adj:
            params["s_adj"] = s_adj
        url = EUROSTAT_URL.format(ds=spec["ds"]) + urllib.parse.urlencode(params)
        try:
            got = parse_jsonstat(json.loads(get(url)))
        except Exception as e:
            last = e
            continue
        if got:
            return got, s_adj or spec["filters"].get("s_adj")
    raise RuntimeError(f"no data ({last})" if last else "no data")


# ---------------------------------------------------------------- main

def plausible(spec, got):
    if not got:
        return got
    vals = sorted(got.values())
    med = vals[len(vals) // 2]
    if PLAUSIBLE[0] <= med <= PLAUSIBLE[1]:
        return got
    print(f"::warning::{spec['key']}: typical value {med:g} is not a plausible index - not used")
    return {}


def main():
    store = json.loads(OUT_FILE.read_text()) if OUT_FILE.exists() else {}
    fx = update_fx(store.get("fx", {}))

    indices = {}
    for s in INDICES:
        old = store.get("indices", {}).get(s["key"], {})
        meta = {k: s[k] for k in ("group", "country", "label", "source", "freq", "desc")}
        if s.get("off"):
            meta["off"] = True
        if s["source"] == "fred":
            meta["id"] = s["id"]
            meta["link"] = f"https://fred.stlouisfed.org/series/{s['id']}"
        else:
            meta["id"] = s["ds"] + " " + " ".join(f"{k}={v}" for k, v in s["filters"].items() if k != "geo")
            meta["link"] = EUROSTAT_DE.format(ds=s["ds"])
        try:
            if s["source"] == "fred":
                got, adj = fetch_fred(s), "NSA"
            else:
                got, adj = fetch_eurostat(s)
            got = plausible(s, got)
            if got:
                meta["adjusted"] = adj == "SCA"
                print(f"{s['key']:<20} {len(got):>4} {'months' if s['freq'] == 'M' else 'quarters'}, latest {max(got)}  ({s['source']})")
        except Exception as e:
            print(f"::warning::{s['key']}: {e} - stored values kept")
            got = {}
        if got:
            indices[s["key"]] = {**meta, "data": dict(sorted(got.items()))}
        elif old:
            indices[s["key"]] = old          # keep what was fetched earlier

    rates = {}
    for s in RATES:
        old = store.get("rates", {}).get(s["key"], {})
        meta = {k: s[k] for k in ("group", "country", "label", "source", "freq", "desc")}
        meta["link"] = {"eurostat": EUROSTAT_DE.format(ds=s.get("ds", "")),
                        "fred_yoy": f"https://fred.stlouisfed.org/series/{s.get('id')}",
                        "ons": "https://www.ons.gov.uk/economy/inflationandpriceindices/timeseries/d7g7/mm23"}[s["source"]]
        try:
            if s["source"] == "eurostat":
                got, _ = fetch_eurostat(s)
            elif s["source"] == "fred_yoy":
                got = fetch_fred_yoy(s)
            else:
                got = fetch_ons(s)
            vals = sorted(got.values())
            if vals and not PLAUSIBLE_RATE[0] <= vals[len(vals) // 2] <= PLAUSIBLE_RATE[1]:
                print(f"::warning::{s['key']}: values do not look like inflation rates - not used")
                got = {}
            if got:
                print(f"{s['key']:<20} {len(got):>4} months, latest {max(got)} = {got[max(got)]}%  ({s['source']})")
        except Exception as e:
            print(f"::warning::{s['key']}: {e} - stored values kept")
            got = {}
        if got:
            rates[s["key"]] = {**meta, "data": dict(sorted(got.items()))}
        elif old:
            rates[s["key"]] = old

    out = {"fx": fx, "indices": indices, "rates": rates}
    if {k: store.get(k) for k in out} == out:
        print("Nothing new - file left unchanged.")
        return
    out = {"updated": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), **out}
    OUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    OUT_FILE.write_text(json.dumps(out, separators=(",", ":")))
    print(f"Wrote {OUT_FILE} ({OUT_FILE.stat().st_size / 1e6:.2f} MB)")


if __name__ == "__main__":
    sys.exit(main())
