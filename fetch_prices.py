#!/usr/bin/env python3
"""
Flypris-overvågning: henter konkurrentpriser for faste ruter og afgangsdage.

Læser ruterne fra routes.csv, omsætter ISO-uger + ugedage til konkrete
afgangsdatoer, søger hver dato og tilføjer resultatet til en CSV-fil i data/.
Hver kørsel lægger nye rækker til, så historikken bevares.

Datakilde (miljøvariablen PROVIDER):
  serpapi  Google Flights via SerpApi (standard)  -> data/prices_google.csv
  ignav    Ignav                                  -> data/prices.csv

Søgningen er fast defineret, så priserne kan sammenlignes over tid:
  1 voksen, one-way, economy, kun direkte fly, dansk marked (priser i DKK),
  laveste pris uden krav om bagage.

Kræver kun Python 3.9+ (ingen ekstra pakker).

Brug:
    python fetch_prices.py --dry-run      # vis datoer og antal søgninger, ingen API-kald
    python fetch_prices.py --test 2       # 2 rigtige søgninger, vises på skærmen, GEMMES IKKE
    python fetch_prices.py                # fuld kørsel, gemmes i data/prices.csv

API-nøglen læses fra SERPAPI_API_KEY (eller IGNAV_API_KEY) eller fra filen
api_key.txt ved siden af scriptet.
"""

import argparse
import csv
import datetime as dt
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

# Vis fremgang løbende i GitHub-loggen (ellers kommer al tekst først til sidst)
try:
    sys.stdout.reconfigure(line_buffering=True)
except Exception:
    pass

# Antal søgninger der kører samtidig. 4 giver ca. 4x hurtigere kørsel.
WORKERS = 4

try:
    from zoneinfo import ZoneInfo
    LOCAL_TZ = ZoneInfo("Europe/Copenhagen")
except Exception:  # fx Windows uden tzdata
    LOCAL_TZ = None

# Datakilde: "serpapi" (Google Flights, standard) eller "ignav".
# Kan overstyres med miljøvariablen PROVIDER.
PROVIDER = os.environ.get("PROVIDER", "serpapi").strip().lower()

# Fast søgedefinition – ændres den, kan nye og gamle priser ikke sammenlignes:
#   1 voksen, one-way, economy, kun direkte fly, dansk marked, priser i DKK.
PROVIDERS = {
    "serpapi": {
        "key_env": "SERPAPI_API_KEY",
        "url": "https://serpapi.com/search.json",
        "params": {
            "engine": "google_flights",
            "type": "2",            # one-way
            "adults": "1",
            "travel_class": "1",    # economy
            "stops": "1",           # kun direkte fly
            "currency": "DKK",
            "gl": "dk",             # dansk marked
            "hl": "da",
            "deep_search": "true",  # samme resultater som Google Flights i browseren
            "show_hidden": "true",  # medtag også "flere fly"
            "no_cache": "true",     # altid frisk søgning; et genbrugt resultat kan mangle priser
        },
        "file": "prices_google.csv",
        "per_hour": 190,  # Starter-planen tillader 200 i timen; lidt margen
    },
    "ignav": {
        "key_env": "IGNAV_API_KEY",
        "url": "https://ignav.com/api/fares/one-way",
        "params": {
            "adults": 1,
            "cabin_class": "economy",
            "max_stops": 0,
            "allow_self_transfer": False,
            "market": "DK",
        },
        "file": "prices.csv",
        "per_hour": None,
    },
}
if PROVIDER not in PROVIDERS:
    sys.exit(f"Ukendt PROVIDER '{PROVIDER}'. Brug 'serpapi' eller 'ignav'.")
SEARCH_SETTINGS = PROVIDERS[PROVIDER]["params"]
# Når Google Flights mangler prisen på et direkte fly (typisk SAS), søges datoen
# igen i disse markeder i rækkefølge, og kun de manglende priser udfyldes.
# Første gensøgning er i det danske marked igen; derefter norsk og svensk.
FALLBACK_MARKETS = [m.strip() for m in os.environ.get("FALLBACK_MARKETS", "dk,no,se").split(",") if m.strip()]
# Maks. antal ekstra søgninger per kørsel til at udfylde manglende priser (beskytter kvoten)
MAX_PRICE_RETRIES = int(os.environ.get("MAX_PRICE_RETRIES", "30"))
# Maks. søgninger i timen (kan overstyres med MAX_PER_HOUR, fx ved større SerpApi-plan)
PER_HOUR = int(os.environ.get("MAX_PER_HOUR") or PROVIDERS[PROVIDER]["per_hour"] or 0)
EXPECTED_CURRENCY = "DKK"
SCHEDULED_HOUR = 7  # lokal tid (København) for den ugentlige kørsel

HERE = Path(__file__).resolve().parent
ROUTES_FILE = HERE / "routes.csv"
DATA_DIR = HERE / "data"
PRICES_FILE = DATA_DIR / PROVIDERS[PROVIDER]["file"]
ERRORS_FILE = DATA_DIR / "errors.csv"

WEEKDAYS = {
    "man": 1, "mandag": 1, "mon": 1,
    "tir": 2, "tirsdag": 2, "tue": 2,
    "ons": 3, "onsdag": 3, "wed": 3,
    "tor": 4, "torsdag": 4, "thu": 4,
    "fre": 5, "fredag": 5, "fri": 5,
    "lør": 6, "lørdag": 6, "lor": 6, "sat": 6,
    "søn": 7, "søndag": 7, "son": 7, "sun": 7,
}

PRICE_COLUMNS = [
    "observed_at", "search_date", "route", "origin", "destination",
    "departure_date", "departure_week", "departure_weekday", "days_to_departure",
    "carrier", "carrier_codes", "flight_numbers", "stops",
    "departure_time_local", "arrival_time_local", "duration_minutes",
    "price", "currency", "price_status", "cabin_class", "self_transfer",
]

# Fejl hvor resten af kørslen er nytteløs (forkert nøgle, ingen kredit)
FATAL_CODES = {401, 402, 403}
# Midlertidige fejl der prøves igen
RETRY_CODES = {424, 429, 500, 502, 503, 504}


class FatalApiError(Exception):
    pass


class NotSearchableYet(Exception):
    pass


def now_local():
    now = dt.datetime.now(dt.timezone.utc)
    return now.astimezone(LOCAL_TZ) if LOCAL_TZ else now


def load_api_key():
    env_name = PROVIDERS[PROVIDER]["key_env"]
    key = os.environ.get(env_name, "").strip()
    if not key:
        key_file = HERE / "api_key.txt"
        if key_file.exists():
            key = key_file.read_text(encoding="utf-8-sig").strip()
    return key


def week_dates(year, week_from, week_to, weekdays):
    """Alle datoer for de valgte ugedage i ISO-uge week_from..week_to (begge inkl.).

    Understøtter perioder over nytår (fx uge 44 til 17): når week_to < week_from,
    fortsætter perioden ind i året efter.
    """
    dates = []
    y, w = year, week_from
    end_year = year + 1 if week_to < week_from else year
    while True:
        for wd in weekdays:
            try:
                dates.append(dt.date.fromisocalendar(y, w, wd))
            except ValueError:
                pass  # fx uge 53 i et år uden uge 53
        if y == end_year and w == week_to:
            break
        w += 1
        if w > dt.date(y, 12, 28).isocalendar()[1]:
            y, w = y + 1, 1
    return sorted(dates)


def load_routes():
    routes = []
    with open(ROUTES_FILE, encoding="utf-8-sig", newline="") as f:
        for i, row in enumerate(csv.DictReader(f), start=2):
            row = {k.strip(): (v or "").strip() for k, v in row.items() if k}
            if not row.get("origin") or row["origin"].startswith("#"):
                continue
            try:
                days = [WEEKDAYS[d.strip().lower()]
                        for d in row["weekdays"].replace(";", ",").split(",") if d.strip()]
                routes.append({
                    "origin": row["origin"].upper(),
                    "destination": row["destination"].upper(),
                    "dates": week_dates(int(row["year"]), int(row["week_from"]),
                                        int(row["week_to"]), days),
                })
            except (KeyError, ValueError) as e:
                sys.exit(f"Fejl i routes.csv linje {i}: {row} ({e})")
    return routes


def _http(req, retries=4):
    """Kald API'et. Prøver igen ved midlertidige fejl, stopper ved fatale."""
    last_err = None
    for attempt in range(1, retries + 1):
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")[:300]
            last_err = f"HTTP {e.code}: {detail}"
            low = detail.lower()
            if e.code in FATAL_CODES or "run out of searches" in low or "invalid api key" in low:
                raise FatalApiError(last_err)
            if "unsupported_search" in low:
                # Datoen ligger for langt ude (salget er ikke åbnet endnu).
                raise NotSearchableYet("uden for søgbar periode endnu")
            if e.code not in RETRY_CODES:
                break  # fx 400: forkert forespørgsel, nytter ikke at prøve igen
        except Exception as e:  # netværk, timeout
            last_err = str(e)
        time.sleep(10 * attempt)
    raise RuntimeError(last_err)


def search(api_key, origin, destination, date, fresh=False, market=None):
    cfg = PROVIDERS[PROVIDER]
    if PROVIDER == "serpapi":
        q = dict(cfg["params"], departure_id=origin, arrival_id=destination,
                 outbound_date=date.isoformat(), api_key=api_key)
        if market:
            q["gl"] = market  # søg som kunde i et andet land (valuta forbliver DKK)
        if fresh:
            q["no_cache"] = "true"  # ny søgning hos Google, ikke SerpApis cache
        req = urllib.request.Request(cfg["url"] + "?" + urllib.parse.urlencode(q),
                                     headers={"Accept": "application/json"})
        data = _http(req)
        err = data.get("error")
        if err:
            low = err.lower()
            if "hasn't returned any results" in low or "no results" in low:
                return {"itineraries": []}  # ingen direkte fly den dag
            if "run out of searches" in low or "invalid api key" in low:
                raise FatalApiError(err)
            if "outbound_date" in low or "date" in low and ("future" in low or "far" in low or "range" in low):
                raise NotSearchableYet("uden for søgbar periode endnu")
            raise RuntimeError(err)
        return data
    body = json.dumps({"origin": origin, "destination": destination,
                       "departure_date": date.isoformat(), **cfg["params"]}).encode("utf-8")
    req = urllib.request.Request(cfg["url"], data=body, method="POST",
                                 headers={"X-Api-Key": api_key, "Content-Type": "application/json",
                                          "Accept": "application/json"})
    return _http(req)


def _base_row(origin, destination, date, today, observed_at):
    return {
        "observed_at": observed_at,
        "search_date": today.isoformat(),
        "route": f"{origin}-{destination}",
        "origin": origin,
        "destination": destination,
        "departure_date": date.isoformat(),
        "departure_week": date.isocalendar()[1],
        "departure_weekday": date.strftime("%a"),
        "days_to_departure": (date - today).days,
    }


def parse_itineraries(data, origin, destination, date, today, observed_at):
    if PROVIDER == "serpapi":
        return _parse_serpapi(data, origin, destination, date, today, observed_at)
    rows = []
    for it in data.get("itineraries") or []:
        out = it.get("outbound") or {}
        segs = out.get("segments") or []
        price = it.get("price") or {}
        carriers = sorted({s.get("marketing_carrier_code") or "" for s in segs} - {""})
        rows.append({
            **_base_row(origin, destination, date, today, data.get("observed_at") or observed_at),
            "carrier": out.get("carrier") or (segs[0].get("operating_carrier_name") if segs else ""),
            "carrier_codes": "/".join(carriers),
            "flight_numbers": " + ".join(
                f"{s.get('marketing_carrier_code', '')}{s.get('flight_number', '')}" for s in segs),
            "stops": max(len(segs) - 1, 0),
            "departure_time_local": segs[0].get("departure_time_local", "") if segs else "",
            "arrival_time_local": segs[-1].get("arrival_time_local", "") if segs else "",
            "duration_minutes": out.get("duration_minutes", ""),
            "price": price.get("amount", ""),
            "currency": price.get("currency", ""),
            "price_status": price.get("status", ""),
            "cabin_class": it.get("cabin_class", ""),
            "self_transfer": it.get("requires_self_transfer", ""),
        })
    return rows


def _gf_time(s):
    # Google Flights: "2027-06-19 08:10" -> "2027-06-19T08:10:00"
    return (s.replace(" ", "T") + ":00") if s and len(s) == 16 else (s or "")


def _parse_serpapi(data, origin, destination, date, today, observed_at):
    best = {}
    for it in (data.get("best_flights") or []) + (data.get("other_flights") or []):
        segs = it.get("flights") or []
        price = it.get("price")
        if not segs or it.get("layovers"):
            continue  # kun direkte fly
        fnums = [(s.get("flight_number") or "").replace(" ", "") for s in segs]
        codes = sorted({(s.get("flight_number") or "").split(" ")[0] for s in segs} - {""})
        market = it.get("_market")
        row = {
            **_base_row(origin, destination, date, today, observed_at),
            "carrier": segs[0].get("airline", ""),
            "carrier_codes": "/".join(codes),
            "flight_numbers": " + ".join(fnums),
            "stops": len(segs) - 1,
            "departure_time_local": _gf_time((segs[0].get("departure_airport") or {}).get("time")),
            "arrival_time_local": _gf_time((segs[-1].get("arrival_airport") or {}).get("time")),
            "duration_minutes": it.get("total_duration", ""),
            "price": "" if price is None else price,
            "currency": SEARCH_SETTINGS["currency"],
            "price_status": "unavailable" if price is None else (f"market_{market}" if market else ""),
            "cabin_class": "economy",
            "self_transfer": False,
        }
        key = row["flight_numbers"]
        prev = best.get(key)
        if prev is None or (price is not None and (prev["price"] == "" or float(price) < float(prev["price"]))):
            best[key] = row  # samme fly kan optræde flere gange; behold laveste kendte pris
    return sorted(best.values(), key=lambda r: float(r["price"]) if r["price"] != "" else 1e12)


def missing_prices(data):
    """True hvis Google Flights returnerede et direkte fly uden pris."""
    return any(not it.get("layovers") and it.get("flights") and it.get("price") is None
               for it in (data.get("best_flights") or []) + (data.get("other_flights") or []))


def _fnums(it):
    return " + ".join((s.get("flight_number") or "").replace(" ", "") for s in it.get("flights") or [])


def fill_missing_prices(data, other, market):
    """Udfyld manglende priser i `data` med priser for samme fly fra et andet marked.
    Kun fly uden pris røres; danske priser overskrives aldrig. Returnerer antal udfyldte."""
    found = {}
    for it in (other.get("best_flights") or []) + (other.get("other_flights") or []):
        if it.get("layovers") or it.get("price") is None:
            continue
        k = _fnums(it)
        if k not in found or it["price"] < found[k]:
            found[k] = it["price"]
    filled = 0
    for it in (data.get("best_flights") or []) + (data.get("other_flights") or []):
        if not it.get("layovers") and it.get("price") is None and _fnums(it) in found:
            it["price"] = found[_fnums(it)]
            it["_market"] = "" if market == "dk" else market
            filled += 1
    return filled

def append_csv(path, columns, rows):
    if not rows:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    new = not path.exists()
    with open(path, "a", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=columns)
        if new:
            w.writeheader()
        w.writerows(rows)


def already_ran(today):
    """True hvis der allerede er gemt priser fra i dag (undgår dobbeltrækker)."""
    if not PRICES_FILE.exists():
        return False
    with open(PRICES_FILE, encoding="utf-8", newline="") as f:
        return any(r.get("search_date") == today.isoformat() for r in csv.DictReader(f))


def main():
    ap = argparse.ArgumentParser(description="Hent konkurrentpriser for faste ruter.")
    ap.add_argument("--dry-run", action="store_true",
                    help="vis datoer og antal søgninger uden at kalde API'et")
    ap.add_argument("--test", type=int, metavar="N", default=0,
                    help="kør N rigtige søgninger og vis resultatet, men gem intet")
    ap.add_argument("--scheduled", action="store_true",
                    help="planlagt kørsel: kører kun mandag fra kl. 07 dansk tid, én gang per dag")
    ap.add_argument("--force", action="store_true",
                    help="kør selvom der allerede er hentet priser i dag")
    args = ap.parse_args()

    local_now = now_local()
    today = local_now.date()

    if args.scheduled:
        if local_now.isoweekday() != 1 or local_now.hour < SCHEDULED_HOUR:
            print(f"Planlagt kørsel springes over ({local_now:%A %H:%M} dansk tid).")
            return

    routes = load_routes()
    jobs = [(r["origin"], r["destination"], d) for r in routes for d in r["dates"] if d > today]
    if args.test:
        jobs = jobs[: args.test]

    print(f"{len(routes)} rute(r), {len(jobs)} søgninger i denne kørsel")
    print(f"Datakilde: {PROVIDER}  Søgning: {json.dumps(SEARCH_SETTINGS, ensure_ascii=False)}")
    for r in routes:
        ds = [d for d in r["dates"] if d > today]
        if ds:
            print(f"  {r['origin']}-{r['destination']}: {len(ds)} afgange, {ds[0]} til {ds[-1]}")

    if args.dry_run:
        return

    if not args.test and not args.force and already_ran(today):
        print(f"Der er allerede hentet priser i dag ({today}). Intet gjort. Brug --force for at køre igen.")
        return

    api_key = load_api_key()
    if not api_key:
        sys.exit(f"Mangler API-nøgle: sæt {PROVIDERS[PROVIDER]['key_env']} eller læg nøglen i api_key.txt")

    observed_at = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    ok = fails = total_rows = 0
    currencies = set()
    stop = {"fatal": None}
    # Bremse: højst PER_HOUR søgninger i timen, jævnt fordelt
    interval = 3600 / PER_HOUR if PER_HOUR else 0
    pace = {"next": time.monotonic(), "lock": threading.Lock()}
    if interval:
        print(f"Bremse: højst {PER_HOUR} søgninger i timen "
              f"(ca. {len(jobs) * interval / 60:.0f} min for denne kørsel)")

    retries = {"left": MAX_PRICE_RETRIES, "used": 0, "filled": 0, "lock": threading.Lock()}

    def wait_turn():
        if interval:
            with pace["lock"]:
                wait = pace["next"] - time.monotonic()
                pace["next"] = max(pace["next"], time.monotonic()) + interval
            if wait > 0:
                time.sleep(wait)

    def run_one(job):
        o, d, date = job
        if stop["fatal"]:
            return None
        wait_turn()
        if stop["fatal"]:
            return None
        try:
            data = search(api_key, o, d, date)
            if PROVIDER == "serpapi":
                for market in FALLBACK_MARKETS:
                    if not missing_prices(data):
                        break
                    with retries["lock"]:
                        ok_retry = retries["left"] > 0
                        if ok_retry:
                            retries["left"] -= 1
                            retries["used"] += 1
                    if not ok_retry:
                        break
                    wait_turn()
                    try:
                        n = fill_missing_prices(data, search(api_key, o, d, date, market=market), market)
                        with retries["lock"]:
                            retries["filled"] += n
                    except FatalApiError:
                        raise
                    except Exception:
                        pass  # behold det, vi har
            return data
        except FatalApiError as e:
            stop["fatal"] = e
            return e
        except Exception as e:
            return e

    # Søgningerne kører parallelt, men resultaterne gemmes i fast rækkefølge
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        results = pool.map(run_one, jobs)
        for n, ((o, d, date), result) in enumerate(zip(jobs, results), start=1):
            if stop["fatal"]:
                pool.shutdown(wait=False, cancel_futures=True)
                break
            if result is None:
                continue
            ok, fails, total_rows = handle_result(
                n, len(jobs), o, d, date, result, args, today, observed_at,
                currencies, ok, fails, total_rows)

    if retries["used"]:
        print(f"Ekstra søgninger for manglende priser: {retries['used']}, udfyldte priser: {retries['filled']}")
    if stop["fatal"]:
        print(f"\nGemt indtil stop: {ok} søgninger OK, {total_rows} prisrækker")
        sys.exit(f"Stoppet: {stop['fatal']}\n(forkert API-nøgle, eller ingen søgninger tilbage på kontoen)")

    where = "intet gemt (test)" if args.test else f"gemt i {PRICES_FILE.relative_to(HERE)}"
    print(f"\nFærdig: {ok} søgninger OK, {fails} fejl, {total_rows} prisrækker, {where}")
    if currencies and currencies != {EXPECTED_CURRENCY}:
        print(f"ADVARSEL: priser kom i {', '.join(sorted(currencies))}, forventet {EXPECTED_CURRENCY}")
    if jobs and not ok:
        sys.exit(1)


def handle_result(n, total, o, d, date, result, args, today, observed_at,
                  currencies, ok, fails, total_rows):
    """Gem (eller vis) resultatet af én søgning. Returnerer opdaterede tællere."""
    label = f"[{n}/{total}] {o}-{d} {date}"
    if isinstance(result, NotSearchableYet):
        print(f"{label}: springes over ({result})")
        return ok, fails, total_rows
    if isinstance(result, Exception):
        if not args.test:
            append_csv(ERRORS_FILE, ["observed_at", "route", "departure_date", "error"],
                       [{"observed_at": observed_at, "route": f"{o}-{d}",
                         "departure_date": date.isoformat(), "error": str(result)}])
        print(f"{label}: FEJL {result}")
        return ok, fails + 1, total_rows

    rows = parse_itineraries(result, o, d, date, today, observed_at)
    currencies.update(r["currency"] for r in rows if r["currency"])
    print(f"{label}: {len(rows)} direkte fly")
    if args.test:
        for r in rows:
            print(f"     {r['price'] or 'pris ukendt'} {r['currency']}  {r['carrier']} {r['flight_numbers']}  "
                  f"afg. {r['departure_time_local'][11:16]}")
    else:
        append_csv(PRICES_FILE, PRICE_COLUMNS, rows)
    return ok + 1, fails, total_rows + len(rows)


if __name__ == "__main__":
    main()
