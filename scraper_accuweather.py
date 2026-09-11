"""
AccuWeather Scraper — Scrapfly version (asp=True, no render_js needed)
================================================================
Alternative US-city source to scraper_weathercom.py. AccuWeather's pages
are server-rendered (unlike weather.com's client-rendered React app), so
Scrapfly's bot-detection bypass (asp=True) is enough on its own — no
render_js needed, which makes these requests cheaper than the weather.com
ones. Plain `requests` gets blocked with a 403 (Akamai-style bot check on
TLS/header fingerprint), confirmed by testing — asp=True clears it.

REQUIRES the SCRAPFLY_API_KEY environment variable — never hardcode it,
this repo is public.
  - Locally:        export SCRAPFLY_API_KEY="your_key_here"
  - GitHub Actions:  add it as a repo secret, referenced in scrape.yml

Writes into the SAME data/ files as scraper.py and scraper_weathercom.py,
using the SAME JSON schema/keys, so api/weather.py needs zero changes:
  data/weather.json          <- merged with other cities' data (not overwritten)
  data/cities/{city}.json    <- one per US city

UNIT / NOMENCLATURE ENGINEERING (the actual point of this rewrite):
  - AccuWeather shows everything in Fahrenheit and miles (US-native site).
    Met Office's schema fields are Celsius/km, so EVERY temperature here
    is converted °F -> °C, and visibility miles -> km, before writing.
    (scraper_weathercom.py currently does NOT do this conversion — it
    dumps raw Fahrenheit into a field named temp_c. Worth fixing there
    too if you keep both scrapers.)
  - AccuWeather's "RealFeel®" = Met Office's "feels like". AccuWeather
    gives a separate Day AND Night RealFeel, so feels_like_low is
    actually populated here (weather.com always left it null).
  - AccuWeather gives a real "Wind Gusts" figure distinct from sustained
    wind, so wind_gust_mph is now an actual gust reading (weather.com
    had to fake this with sustained wind since it doesn't expose gust).
  - uv_level / air_pollution: AccuWeather shows "6.0 (High)" style —
    we keep just the descriptive word ("High") to match Met Office's
    "Low"/"High" style strings.
  - air_pollution has no equivalent on AccuWeather's current-weather
    page at all — it only appears per-hour on the hourly page, so we
    take the nearest (first) hour's reading as "now".

To add a US city: add one entry to CITIES below. AccuWeather URLs look
like:
  https://www.accuweather.com/en/us/{state-slug}/{zip}/current-weather/{location_id}
Find the {state-slug}/{zip}/{location_id} triple by searching the city
on accuweather.com and copying it out of the URL.
"""

import json
import os
import re
import sys
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed

from bs4 import BeautifulSoup
from scrapfly import ScrapflyClient, ScrapeConfig

# ─────────────────────────────────────────────
#  ADD / REMOVE US CITIES HERE
#  "path" = "{state-slug}/{zip}", "location_id" = the numeric id in the URL
# ─────────────────────────────────────────────
CITIES = {
    "New York":      {"path": "new-york/10021",    "location_id": "349727", "country": "US"},
    "Los Angeles":   {"path": "los-angeles/90012",  "location_id": "347625", "country": "US"},
    "Chicago":       {"path": "chicago/60608",      "location_id": "348308", "country": "US"},
    "Dallas":        {"path": "dallas/75202",       "location_id": "351194", "country": "US"},
    "Miami":         {"path": "miami/33128",        "location_id": "347936", "country": "US"},
    "Washington DC": {"path": "washington/20006",   "location_id": "327659", "country": "US"},
    "Boston":        {"path": "boston/02108",       "location_id": "348735", "country": "US"},

    # Houston, Atlanta, Phoenix intentionally dropped from the US list.
}

MAX_WORKERS = 5
SCRAPFLY_API_KEY = os.environ.get("SCRAPFLY_API_KEY")
if not SCRAPFLY_API_KEY:
    sys.exit("SCRAPFLY_API_KEY environment variable is not set. "
              "Set it before running: export SCRAPFLY_API_KEY=your_key_here")
client = ScrapflyClient(key=SCRAPFLY_API_KEY)


# ─────────────────────────────────────────────
#  UNIT CONVERSION HELPERS
# ─────────────────────────────────────────────

def to_int(s):
    if s is None:
        return None
    m = re.search(r"-?\d+", str(s))
    return int(m.group()) if m else None


def f_to_c(f):
    """Fahrenheit -> Celsius, rounded to nearest int. None-safe."""
    if f is None:
        return None
    return round((f - 32) * 5 / 9)


def mi_to_km_str(mi_text):
    """'10 mi' -> '16km' (Met Office visibility string style). None-safe."""
    mi = to_int(mi_text)
    if mi is None:
        return None
    return f"{round(mi * 1.60934)}km"


def extract_parenthetical(text):
    """'6.0 (High)' -> 'High'. None-safe."""
    if not text:
        return None
    m = re.search(r"\(([^)]+)\)", text)
    return m.group(1).strip() if m else text.strip()


def infer_date_from_mmdd(mmdd, ref=None):
    """'9/12' -> '2026-09-12', rolling into next year if it's already past."""
    ref = ref or datetime.now(timezone.utc)
    try:
        month, day = (int(x) for x in mmdd.strip().split("/"))
    except (ValueError, AttributeError):
        return None
    year = ref.year
    candidate = datetime(year, month, day, tzinfo=timezone.utc)
    if (ref - candidate).days > 180:
        candidate = datetime(year + 1, month, day, tzinfo=timezone.utc)
    return candidate.strftime("%Y-%m-%d")


def get_p_value(container, label):
    """
    AccuWeather's common <p>Label<span class="value">Value</span></p>
    pattern (used in half-day-card panels and hourly detail panels).
    """
    if container is None:
        return None
    for p in container.find_all("p"):
        val = p.select_one(".value")
        if not val:
            continue
        lbl = p.get_text(" ", strip=True).replace(val.get_text(strip=True), "").strip()
        if lbl == label:
            return val.get_text(strip=True)
    return None


def get_detail_item(soup, label):
    """
    current-weather page's <div class="detail-item"><div>Label</div>
    <div>Value</div></div> pattern.
    """
    for item in soup.select(".detail-item"):
        divs = item.find_all("div", recursive=False)
        if len(divs) == 2 and divs[0].get_text(strip=True) == label:
            return divs[1].get_text(strip=True)
    return None


# ─────────────────────────────────────────────
#  FETCH
# ─────────────────────────────────────────────

def fetch_html(url: str) -> str:
    """
    AccuWeather is server-rendered, so render_js isn't needed — asp=True
    alone clears its bot-detection (confirmed: plain requests gets a 403,
    Scrapfly asp=True gets the full real page).
    """
    config = ScrapeConfig(url=url, asp=True, country="US")
    result = client.scrape(config)
    return result.content


# ─────────────────────────────────────────────
#  PARSE current-weather PAGE
# ─────────────────────────────────────────────

def parse_current(html: str, url: str) -> dict:
    soup = BeautifulSoup(html, "html.parser")
    out = {
        "url": url, "condition": None, "temp_c": None, "feels_like": None,
        "temp_max": None, "temp_min": None, "rain_chance": None,
        "wind_mph": None, "humidity_pct": None, "uv_level": None,
        "visibility": None, "sunrise": None, "sunset": None,
        "day_condition": None, "feels_like_day": None, "feels_like_night": None,
        "wind_gust_mph": None,
    }

    # Current temp + condition
    disp = soup.select_one(".display-temp")
    out["temp_c"] = f_to_c(to_int(disp.get_text())) if disp else None

    cur_card = soup.select_one(".current-weather-card")
    cur_phrase = cur_card.select_one(".phrase") if cur_card else None
    out["condition"] = cur_phrase.get_text(strip=True) if cur_phrase else None

    # detail-item strip: RealFeel, Wind, Wind Gusts, Humidity, Visibility
    out["feels_like"] = f_to_c(to_int(get_detail_item(soup, "RealFeel®")))
    out["wind_mph"] = to_int(get_detail_item(soup, "Wind"))
    out["wind_gust_mph_current"] = to_int(get_detail_item(soup, "Wind Gusts"))  # real gust, not sustained
    out["humidity_pct"] = to_int(get_detail_item(soup, "Humidity"))
    out["visibility"] = mi_to_km_str(get_detail_item(soup, "Visibility"))

    rain_m = re.search(r"Probability of Precipitation\s*<[^>]*>(\d+%)", str(soup))
    # (fallback below via half-day-card panel-items instead — more reliable)

    # Day / Night half-day-cards
    for card in soup.select(".half-day-card"):
        title_el = card.select_one(".title")
        title = title_el.get_text(strip=True) if title_el else ""

        temp_el = card.select_one(".half-day-card-header .temperature")
        hi_lo = to_int(temp_el.get_text()) if temp_el else None

        rf_div = card.select_one(".real-feel")
        rf_m = re.search(r"RealFeel®\s*(-?\d+)", rf_div.get_text(" ", strip=True)) if rf_div else None
        realfeel = to_int(rf_m.group(1)) if rf_m else None

        phrase = card.select_one(".phrase")
        condition = phrase.get_text(strip=True) if phrase else None

        gusts = to_int(get_p_value(card, "Wind Gusts"))
        pop = get_p_value(card, "Probability of Precipitation")
        uv_raw = get_p_value(card, "Max UV Index")

        if title == "Day":
            out["temp_max"] = f_to_c(hi_lo)
            out["feels_like_day"] = f_to_c(realfeel)
            out["day_condition"] = condition
            out["wind_gust_mph"] = gusts
            out["rain_chance"] = pop
            out["uv_level"] = extract_parenthetical(uv_raw)
        elif title == "Night":
            out["temp_min"] = f_to_c(hi_lo)
            out["feels_like_night"] = f_to_c(realfeel)

    # Sunrise/Sunset — first Rise/Set pair is today's
    times = soup.select(".sunrise-sunset__times-item")
    for t in times[:2]:
        label = t.select_one(".sunrise-sunset__times-label")
        value = t.select_one(".sunrise-sunset__times-value")
        if not label or not value:
            continue
        if label.get_text(strip=True) == "Rise":
            out["sunrise"] = value.get_text(strip=True)
        elif label.get_text(strip=True) == "Set":
            out["sunset"] = value.get_text(strip=True)

    return out


# ─────────────────────────────────────────────
#  PARSE hourly-weather-forecast PAGE
# ─────────────────────────────────────────────

def parse_hourly(html: str) -> list:
    soup = BeautifulSoup(html, "html.parser")
    hourly = []
    air_quality_seen = None

    for row in soup.select(".accordion-item.hour"):
        time_el = row.select_one(".hourly-card-top .date")
        temp_el = row.select_one(".hourly-card-top .temp")
        phrase_el = row.select_one(".phrase")
        precip_el = row.select_one(".hourly-card-top .precip")

        header_panel = row.select_one(".hourly-detailed-card-header .panel.no-realfeel-phrase")
        wind_mph = to_int(get_p_value(header_panel, "Wind"))
        aq = get_p_value(header_panel, "Air Quality")
        if aq and air_quality_seen is None:
            air_quality_seen = aq  # first (nearest) hour — used as "current" air quality

        hourly.append({
            "time":        time_el.get_text(strip=True) if time_el else None,
            "temp_c":      f_to_c(to_int(temp_el.get_text())) if temp_el else None,
            "condition":   phrase_el.get_text(strip=True) if phrase_el else None,
            "rain_chance": precip_el.get_text(strip=True) if precip_el else None,
            "wind_mph":    wind_mph,
        })

    return hourly, air_quality_seen


# ─────────────────────────────────────────────
#  PARSE 10-day-weather-forecast PAGE
# ─────────────────────────────────────────────

def parse_forecast(html: str) -> list:
    soup = BeautifulSoup(html, "html.parser")
    forecast = []

    wrappers = soup.select(".daily-wrapper")
    for w in wrappers[1:]:  # skip index 0 — that's "today", already covered by parse_current()
        card = w.select_one(".daily-forecast-card")
        if not card:
            continue

        sub_date = card.select_one(".sub.date")
        date_str = infer_date_from_mmdd(sub_date.get_text(strip=True)) if sub_date else None

        high_el = card.select_one(".temp .high")
        low_el = card.select_one(".temp .low")
        hi = to_int(high_el.get_text()) if high_el else None
        lo = to_int(low_el.get_text()) if low_el else None

        phrase = w.select_one(".half-day-card-content .phrase")
        condition = phrase.get_text(strip=True) if phrase else None

        forecast.append({
            "date":      date_str,
            "condition": condition,
            "temp_min":  f_to_c(lo),
            "temp_max":  f_to_c(hi),
        })

    return forecast


# ─────────────────────────────────────────────
#  SCRAPE ONE CITY (3 Scrapfly requests: current, hourly, tenday)
# ─────────────────────────────────────────────

def scrape_city(city_name: str, path: str, location_id: str, country: str = "US") -> dict:
    print(f"  Fetching {city_name}...")
    current_url = f"https://www.accuweather.com/en/us/{path}/current-weather/{location_id}"
    hourly_url = f"https://www.accuweather.com/en/us/{path}/hourly-weather-forecast/{location_id}"
    tenday_url = f"https://www.accuweather.com/en/us/{path}/10-day-weather-forecast/{location_id}"

    try:
        current = parse_current(fetch_html(current_url), current_url)
        hourly, air_quality = parse_hourly(fetch_html(hourly_url))
        forecast = parse_forecast(fetch_html(tenday_url))
    except Exception as e:
        print(f"  ✗ Error: {city_name} — {e}")
        return {
            "city": city_name,
            "geohash": f"{path}/{location_id}",
            "country": country,
            "source": "AccuWeather",
            "error": str(e),
            "scraped_at": datetime.now(timezone.utc).isoformat(),
        }

    today_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    today_block = {
        "date":              today_date,
        "condition":         current["day_condition"] or current["condition"],
        "temp_max":          current["temp_max"],
        "temp_min":          current["temp_min"],
        "feels_like_high":   current["feels_like_day"],
        "feels_like_low":    current["feels_like_night"],
        "wind_gust_mph":     current["wind_gust_mph"],
        "humidity_high_pct": current["humidity_pct"],
        "humidity_low_pct":  None,
        "uv_level":          current["uv_level"],
        "visibility_high":   current["visibility"],
        "visibility_low":    None,
        "air_pollution":     air_quality,
        "pollen":            None,
        "sunrise":           current["sunrise"],
        "sunset":            current["sunset"],
        "current": {
            "temp_c":        current["temp_c"],
            "condition":     current["condition"],
            "feels_like":    current["feels_like"],
            "rain_chance":   current["rain_chance"],
            "wind_gust_mph": current["wind_gust_mph_current"],  # real gust, not sustained wind
            "pollen":        None,
        },
        "hourly":   hourly,
        "warnings": [],
    }

    print(f"  ✓ {city_name}")
    return {
        "city":       city_name,
        "geohash":    f"{path}/{location_id}",
        "country":    country,
        "source":     "AccuWeather",
        "source_url": current["url"],
        "scraped_at": datetime.now(timezone.utc).isoformat(),
        "today":      today_block,
        "forecast":   forecast,
    }


# ─────────────────────────────────────────────
#  MAIN — scrape all US cities concurrently, merge into shared data/ files
# ─────────────────────────────────────────────

def main():
    print(f"\n{'='*50}")
    print(f"  AccuWeather Scraper (Scrapfly, asp=True)")
    print(f"  {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
    print(f"  Cities: {len(CITIES)}")
    print(f"{'='*50}\n")

    os.makedirs("data/cities", exist_ok=True)

    scraped = []
    failed = []

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        futures = {
            pool.submit(scrape_city, city_name, meta["path"], meta["location_id"], meta.get("country", "US")): city_name
            for city_name, meta in CITIES.items()
        }
        for future in as_completed(futures):
            data = future.result()
            city_name = data["city"]
            city_slug = city_name.lower().replace(" ", "_")

            if "error" in data:
                failed.append(city_name)

            scraped.append(data)

            with open(f"data/cities/{city_slug}.json", "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)

    combined_path = "data/weather.json"
    if os.path.exists(combined_path):
        with open(combined_path, encoding="utf-8") as f:
            combined = json.load(f)
    else:
        combined = {"generated_at": None, "total_cities": 0, "cities": []}

    scraped_names = {c["city"] for c in scraped}
    combined["cities"] = [c for c in combined["cities"] if c["city"] not in scraped_names] + scraped
    combined["generated_at"] = datetime.now(timezone.utc).isoformat()
    combined["total_cities"] = len(combined["cities"])

    with open(combined_path, "w", encoding="utf-8") as f:
        json.dump(combined, f, indent=2, ensure_ascii=False)

    print(f"\n{'='*50}")
    print(f"  Done. {len(scraped) - len(failed)}/{len(scraped)} scraped.")
    if failed:
        print(f"  Failed: {', '.join(failed)}")
    print(f"  Output: data/weather.json (merged) + data/cities/*.json")
    print(f"{'='*50}\n")


if __name__ == "__main__":
    main()
