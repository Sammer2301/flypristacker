# Flypris-overvågning – proof of concept

Henter hver mandag kl. 07 konkurrenternes laveste priser fra Google Flights (via SerpApi) for faste ruter og afgangsdage og gemmer dem, så I kan følge prisudviklingen frem mod afgang.

## Opsætning

- **Ruter (5, kun udrejse):** CPH–CHQ, CPH–LCA, CPH–PMI, BLL–CHQ, BLL–PMI
- **Afgange:** torsdage og lørdage i uge 18–39 2027 (6. maj – 2. oktober 2027), 44 per rute
- **Søgning:** 1 voksen, one-way, economy, kun direkte fly, ingen selvtransfer, dansk marked, priser i DKK, laveste pris uden krav om bagage
- **Omfang:** 220 søgninger per kørsel, ca. 950 om måneden. Passer i SerpApi Starter ($25/md., 1.000 søgninger, 200 i timen). Scriptet holder sig under 190 søgninger i timen, så en kørsel tager ca. 70 minutter.
- **Data:** `data/prices_google.csv`.

Søgedefinitionen ligger fast i toppen af `fetch_prices.py`. Ændres den undervejs, kan nye og gamle priser ikke sammenlignes.

## Filer

| Fil | Hvad |
|---|---|
| `routes.csv` | Ruterne. Én linje per rute. |
| `fetch_prices.py` | Henter priserne og gemmer dem. |
| `data/prices_google.csv` | Historikken. Én række per fly per søgning. Oprettes ved første fulde kørsel. |
| `data/errors.csv` | Søgninger der fejlede (oprettes kun hvis noget fejler). |
| `.github/workflows/weekly.yml` | Kører det automatisk hver mandag kl. 07 via GitHub. |

## Opsætning på GitHub

1. Opret et **privat** repository, fx `flypris`.
2. Upload alle filerne inkl. mappen `.github`. Upload **ikke** `api_key.txt`.
3. *Settings → Secrets and variables → Actions → New repository secret*: navn `SERPAPI_API_KEY`, værdi = din nøgle fra serpapi.com.

## Kørsel

Fanen *Actions* → *Ugentlig prishentning* → *Run workflow* → vælg:

- **test** – 2 søgninger. Priserne vises i loggen, men **intet gemmes**. Brug den til at tjekke, at alt virker.
- **fuld** – rigtig kørsel med alle 220 søgninger. Gemmes i `data/prices_google.csv`.

Herefter kører den selv hver mandag kl. 07 dansk tid.

Sikkerhed mod rod i data:
- En test gemmer aldrig noget.
- Der kan kun gemmes én kørsel per dag. Starter man en ekstra kørsel samme dag, gør den ingenting.
- Afgange, der er passeret, springes automatisk over.

## Kolonner i prices.csv

`search_date` (hvornår prisen blev hentet), `route`, `departure_date`, `departure_week`, `days_to_departure`, `carrier`, `carrier_codes`, `flight_numbers`, `departure_time_local`, `price`, `currency`, `price_status` m.fl.

Prisudviklingen fås ved at tage den laveste `price` per `carrier` og `departure_date` og plotte den mod `days_to_departure`.

## Fejl

- **Stoppet: HTTP 401/403** – API-nøglen er forkert eller mangler i GitHub Secrets.
- **Stoppet: HTTP 402** – kontoen hos Ignav er løbet tør for søgninger. Tilføj betaling hos Ignav.
- **ADVARSEL: priser kom i USD** – markedsindstillingen virker ikke som forventet. Kontakt den, der har sat det op.

## Køre lokalt (valgfrit)

Kræver Python 3.9+. Læg nøglen i `api_key.txt` og kør `python fetch_prices.py --dry-run`, `--test 2` eller uden argumenter.

## Forbehold

- Priserne er den laveste pris datakilden finder – ikke billetklasser eller restpladser.
- Selskaber, der kun sælger pakkerejser, er typisk ikke med.
