#!/usr/bin/env python3
"""
Flypris-overvågning: henter konkurrentpriser for faste ruter og afgangsdage.

Læser ruterne fra routes.csv, omsætter ISO-uger + ugedage til konkrete
afgangsdatoer, søger hver dato hos Ignav og tilføjer resultatet til
data/prices.csv. Hver kørsel lægger nye rækker til, så historikken bevares.

Søgningen er fast defineret, så priserne kan sammenlignes over tid:
  1 voksen, one-way, economy, kun direkte fly, ingen selvtransfer,
  dansk marked (priser i DKK), laveste pris uden krav om bagage.

Kræver kun Python 3.9+ (ingen ekstra pakker).

Brug:
    python fetch_prices.py --dry-run      # vis datoer og antal søgninger, ingen API-kald
    python fetch_prices.py --test 2       # 2 rigtige søgninger, vises på skærmen, GEMMES IKKE
    python fetch_prices.py                # fuld kørsel, gemmes i data/prices.csv

API-nøglen læses fra miljøvariablen IGNAV_API_KEY eller fra filen api_key.txt
ved siden af scriptet.
"""

import argparse
import csv
import datetime as dt
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

try:
    from zoneinfo import ZoneInfo
    LOCAL_TZ = ZoneInfo("Europe/Copenhagen")
except Exception:  # fx Windows uden tzdata
    LOCAL_TZ = None

API_URL = "https://ignav.com/api/fares/one-way"

# Fast søgedefinition – ændres den, kan nye og gamle priser ikke sammenlignes.
SEARCH_SETTINGS = {
    "adults": 1,
    "cabin_class": "economy",
    "max_stops": 0,               # kun direkte fly
    "allow_self_transfer": False,  # kun rigtige tilbud fra ét selskab
    "market": "DK",               # dansk kunde, priser i DKK
}
EXPECTED_CURRENCY = "DKK"
SCHEDULED_HOUR = 7  # lokal tid (København) for den ugentlige kørsel

HERE = Path(__file__).resolve().parent
ROUTES_FILE = HERE / "routes.csv"
DATA_DIR = HERE / "data"
PRICES_FILE = DATA_DIR / "prices.csv"
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


def now_local():
    now = dt.datetime.now(dt.timezone.utc)
    return now.astimezone(LOCAL_TZ) if LOCAL_TZ else now


def load_api_key():
    key = os.environ.get("IGNAV_API_KEY", "").strip()
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


def search(api_key, origin, destination, date, retries=4):
    body = json.dumps({
        "origin": origin,
        "destination": destination,
        "departure_date": date.isoformat(),
        **SEARCH_SETTINGS,
    }).encode("utf-8")
    req = urllib.request.Request(
        API_URL, data=body, method="POST",
        headers={"X-Api-Key": api_key, "Content-Type": "application/json",
                 "Accept": "application/json"},
    )
    last_err = None
    for attempt in range(1, retries + 1):
        try:
            with urllib.request.urlopen(req, timeout=90) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")[:300]
            last_err = f"HTTP {e.code}: {detail}"
            if e.code in FATAL_CODES:
                raise FatalApiError(last_err)
            if e.code not in RETRY_CODES:
                break  # fx 400: forkert forespørgsel, nytter ikke at prøve igen
        except Exception as e:  # netværk, timeout
            last_err = str(e)
        time.sleep(5 * attempt)
    raise RuntimeError(last_err)


def parse_itineraries(data, origin, destination, date, today, observed_at):
    rows = []
    for it in data.get("itineraries") or []:
        out = it.get("outbound") or {}
        segs = out.get("segments") or []
        price = it.get("price") or {}
        carriers = sorted({s.get("marketing_carrier_code") or "" for s in segs} - {""})
        rows.append({
            "observed_at": data.get("observed_at") or observed_at,
            "search_date": today.isoformat(),
            "route": f"{origin}-{destination}",
            "origin": origin,
            "destination": destination,
            "departure_date": date.isoformat(),
            "departure_week": date.isocalendar()[1],
            "departure_weekday": date.strftime("%a"),
            "days_to_departure": (date - today).days,
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
    print(f"Søgning: {json.dumps(SEARCH_SETTINGS, ensure_ascii=False)}")
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
        sys.exit("Mangler API-nøgle: sæt IGNAV_API_KEY eller læg nøglen i api_key.txt")

    observed_at = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    ok = fails = total_rows = 0
    currencies = set()
    for n, (o, d, date) in enumerate(jobs, start=1):
        try:
            data = search(api_key, o, d, date)
            rows = parse_itineraries(data, o, d, date, today, observed_at)
            currencies.update(r["currency"] for r in rows if r["currency"])
            total_rows += len(rows)
            ok += 1
            if args.test:
                print(f"[{n}/{len(jobs)}] {o}-{d} {date}: {len(rows)} direkte fly")
                for r in sorted(rows, key=lambda r: float(r["price"] or 0)):
                    print(f"     {r['price']} {r['currency']}  {r['carrier']} {r['flight_numbers']}  "
                          f"afg. {r['departure_time_local'][11:16]}")
            else:
                append_csv(PRICES_FILE, PRICE_COLUMNS, rows)
                print(f"[{n}/{len(jobs)}] {o}-{d} {date}: {len(rows)} direkte fly")
        except FatalApiError as e:
            sys.exit(f"Stoppet: {e}\n(401/403 = forkert API-nøgle, 402 = ingen søgninger tilbage på kontoen)")
        except Exception as e:
            fails += 1
            if not args.test:
                append_csv(ERRORS_FILE, ["observed_at", "route", "departure_date", "error"],
                           [{"observed_at": observed_at, "route": f"{o}-{d}",
                             "departure_date": date.isoformat(), "error": str(e)}])
            print(f"[{n}/{len(jobs)}] {o}-{d} {date}: FEJL {e}")
        time.sleep(0.5)

    where = "intet gemt (test)" if args.test else f"gemt i {PRICES_FILE.relative_to(HERE)}"
    print(f"\nFærdig: {ok} søgninger OK, {fails} fejl, {total_rows} prisrækker, {where}")
    if currencies and currencies != {EXPECTED_CURRENCY}:
        print(f"ADVARSEL: priser kom i {', '.join(sorted(currencies))}, forventet {EXPECTED_CURRENCY}")
    if jobs and not ok:
        sys.exit(1)


if __name__ == "__main__":
    main()
