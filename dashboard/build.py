#!/usr/bin/env python3
"""
Bygger dashboardet (docs/index.html) ud fra data/prices_google.csv.

Brug:  python dashboard/build.py [data/prices_google.csv] [docs/index.html]

Kaldes automatisk af GitHub-kørslen hver mandag efter prishentningen,
så siden på GitHub Pages altid viser de nyeste priser.
"""
import csv
import datetime as dt
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
SRC = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "data" / "prices_google.csv"
OUT = Path(sys.argv[2]) if len(sys.argv) > 2 else ROOT / "docs" / "index.html"

NAMES = {"SK": "SAS", "D8": "Norwegian", "DY": "Norwegian", "FR": "Ryanair"}
WEEKDAYS = {"man": 1, "tir": 2, "ons": 3, "tor": 4, "fre": 5, "lør": 6, "søn": 7}


def load_routes():
    """Ruter og afgangsdatoer læses fra routes.csv, så dashboardet følger opsætningen."""
    routes, dates = [], set()
    with open(ROOT / "routes.csv", encoding="utf-8-sig", newline="") as f:
        for r in csv.DictReader(f):
            if not r.get("origin") or r["origin"].startswith("#"):
                continue
            routes.append(f'{r["origin"].strip().upper()}-{r["destination"].strip().upper()}')
            days = [WEEKDAYS[d.strip().lower()[:3]] for d in r["weekdays"].replace(";", ",").split(",") if d.strip()]
            y, w0, w1 = int(r["year"]), int(r["week_from"]), int(r["week_to"])
            for w in range(w0, w1 + 1):
                for d in days:
                    dates.add(dt.date.fromisocalendar(y, w, d).isoformat())
    return routes, sorted(dates)


def main():
    rows = []
    with open(SRC, encoding="utf-8", newline="") as f:
        for r in csv.DictReader(f):
            code = r["carrier_codes"].split("/")[0]
            status = r.get("price_status", "")
            rows.append([
                r["search_date"], r["route"], r["departure_date"],
                NAMES.get(code, r["carrier"]), r["flight_numbers"],
                r["departure_time_local"][11:16], r["arrival_time_local"][11:16],
                round(float(r["price"])) if r["price"] else None,
                status.replace("market_", "").upper() if status.startswith("market_") else "",
            ])
    routes, dates = load_routes()
    data = {"rows": rows, "routes": routes, "dates": dates}
    body = (HERE / "template.html").read_text(encoding="utf-8").replace(
        "__DATA__", json.dumps(data, ensure_ascii=False, separators=(",", ":")))
    page = (
        "<!doctype html>\n<html lang=\"en\">\n<head>\n<meta charset=\"utf-8\">\n"
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1, viewport-fit=cover\">\n"
        "<meta name=\"robots\" content=\"noindex, nofollow\">\n"
        "<style>html{-webkit-text-size-adjust:100%}body{margin:0;font-size:14px;font-family:system-ui,sans-serif}"
        "img{max-width:100%}[hidden]{display:none!important}"
        ":root{padding-top:env(safe-area-inset-top,0px);padding-bottom:env(safe-area-inset-bottom,0px)}</style>\n"
        "</head>\n<body>\n" + body + "\n</body>\n</html>\n"
    )
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(page, encoding="utf-8")
    print(f"Dashboard bygget: {OUT} ({len(rows)} prisrækker)")


if __name__ == "__main__":
    main()
