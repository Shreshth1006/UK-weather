"""
Met Office Weather Scraper → Static JSON API
============================================
Scrapes weather.metoffice.gov.uk for UK cities and writes:
  data/weather.json          ← all cities combined
  data/cities/{city}.json    ← one file per city

To add a new city: just add it to CITIES dict below.
GitHub Pages serves these as a free static API.
"""

import requests
import json
import os
import re
from bs4 import BeautifulSoup
from datetime import datetime

# ─────────────────────────────────────────────
#  ADD / REMOVE CITIES HERE
#  Format: "Display Name": "geohash"
# ─────────────────────────────────────────────
CITIES = {
    "London":         "gcpvj0v07",
    "Manchester":     "gcw2hzs1u",
    "Birmingham":     "gcqyd2ux3",
    "Glasgow":        "gcw2p7ygw",
    "Bristol":        "gcjkmy91s",
    "Leeds":          "gcwfhxhup",
    "Edinburgh":      "gcvwr3zrw",
    "Liverpool":      "gcjvkr6ky",
    "Cardiff":        "gcjszevgx",
    "Sheffield":      "gcqy3k6sz",
    "Belfast":        "gcey94cuf",
    "York":           "gcwf9nwyu",
    "Lewisham":       "gcpvjn89k",
    "Cambridgeshire": "u12esxqub",
    "Nottingham":     "gcqs7kgbx",
    "Oxfordshire":    "gcpue0tpf",
    "Newcastle":      "gcsbptbvu",
}

BASE_URL = "https://weather.metoffice.gov.uk/forecast/{}"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-GB,en;q=0.9",
}


# ─────────────────────────────────────────────
#  HELPERS
# ─────────────────────────────────────────────

def clean_temp(raw: str) -> int | None:
    """'23°' or '23 degrees Celsius' → 23"""
    match = re.search(r"-?\d+", raw)
    return int(match.group()) if match else None


def clean_wind(raw: str) -> int | None:
    """'24mph from W' or '24mph' → 24"""
    match = re.search(r"\d+", raw)
    return int(match.group()) if match else None


def clean_rain(raw: str) -> str:
    """Normalize rain string: '<5%' stays, '50%' stays"""
    raw = raw.strip()
    if raw.startswith("<"):
        return "<5%"
    match = re.search(r"\d+", raw)
    return f"{match.group()}%" if match else raw


def parse_time_str(raw: str) -> str:
    """'5:59am on 15 June 2026' → ISO datetime string"""
    try:
        return datetime.strptime(raw.strip(), "%I:%M%p on %d %B %Y").isoformat()
    except Exception:
        return raw.strip()


# ─────────────────────────────────────────────
#  PARSE DAILY FORECAST CARDS
# ─────────────────────────────────────────────

def parse_daily_cards(soup: BeautifulSoup) -> list[dict]:
    """
    Parses the 7-day forecast strip.
    Each card text looks like:
    'Mon 15 Mon 15 Jun Sunny day; 23° Maximum daytime temperature: 23 degrees Celsius;
     14° Minimum nighttime temperature: 14 degrees Celsius;'
    """
    days = []

    # Cards are anchor tags with href containing date=
    cards = soup.find_all("a", href=re.compile(r"date=\d{4}-\d{2}-\d{2}"))

    # Also check list items / divs that contain date patterns
    if not cards:
        # Fallback: look for text blocks matching the pattern
        all_text = soup.get_text(separator="\n")
        card_pattern = re.compile(
            r"(Today|Mon|Tue|Wed|Thu|Fri|Sat|Sun)\s+\d+\s+\w+\s+\d+\s+\w+\s+([^;]+);\s*"
            r"(\d+)°\s+Maximum[^;]+;\s*(\d+)°\s+Minimum"
        )
        for m in card_pattern.finditer(all_text):
            days.append({
                "day_label": m.group(1),
                "condition": m.group(2).strip(),
                "temp_max": int(m.group(3)),
                "temp_min": int(m.group(4)),
            })
        return days

    for card in cards:
        text = card.get_text(separator=" ", strip=True)
        href = card.get("href", "")

        # Extract date from href
        date_match = re.search(r"date=(\d{4}-\d{2}-\d{2})", href)
        date_str = date_match.group(1) if date_match else None

        # Extract condition
        cond_match = re.search(
            r"(Sunny|Cloudy|Overcast|Rain|Drizzle|Shower|Thunder|Snow|Sleet|Fog|Mist|Clear|Partly)[^\d;]*",
            text, re.IGNORECASE
        )
        condition = cond_match.group(0).strip().rstrip(";") if cond_match else "Unknown"

        # Extract max temp
        max_match = re.search(r"Maximum[^:]*:\s*(\d+)\s*degrees", text, re.IGNORECASE)
        if not max_match:
            max_match = re.search(r"(\d+)°\s*Maximum", text)
        temp_max = int(max_match.group(1)) if max_match else None

        # Extract min temp
        min_match = re.search(r"Minimum[^:]*:\s*(\d+)\s*degrees", text, re.IGNORECASE)
        if not min_match:
            min_match = re.search(r"(\d+)°\s*Minimum", text)
        temp_min = int(min_match.group(1)) if min_match else None

        days.append({
            "date": date_str,
            "condition": condition,
            "temp_max": temp_max,
            "temp_min": temp_min,
        })

    return days


# ─────────────────────────────────────────────
#  PARSE HOURLY FORECAST
# ─────────────────────────────────────────────

def parse_hourly(soup: BeautifulSoup, target_date: str) -> list[dict]:
    """
    Hourly data is in markdown-style tables in the server-rendered HTML.
    Rows: Time | Weather+Temp | Rain% | Wind
    We target the table for target_date (today or a forecast day).
    """
    hourly = []
    text = soup.get_text(separator="\n")

    # Find hourly sections — each day has a header then a table-like block
    # Pattern: time slots like "7am 8am 9am..."
    time_pattern = re.compile(r"(\d{1,2}am|\d{1,2}pm)")

    lines = text.split("\n")
    lines = [l.strip() for l in lines if l.strip()]

    i = 0
    while i < len(lines):
        line = lines[i]

        # Look for a line that is purely time slots
        times = time_pattern.findall(line)
        if len(times) >= 3:
            # Next lines should have: conditions+temps, rain%, wind
            try:
                conditions_line = lines[i + 1] if i + 1 < len(lines) else ""
                rain_line = lines[i + 2] if i + 2 < len(lines) else ""
                wind_line = lines[i + 3] if i + 3 < len(lines) else ""

                # Parse temps from conditions line (numbers followed by °)
                temps = re.findall(r"(-?\d+)°", conditions_line)
                # Parse conditions (words like Sunny, Cloudy etc)
                conds = re.findall(
                    r"(Sunny day|Sunny intervals|Cloudy|Overcast|Clear night|"
                    r"Partly cloudy night|Drizzle|Rain|Shower|Thunder|Snow|Sleet|Fog|Mist)",
                    conditions_line, re.IGNORECASE
                )
                # Parse rain
                rains = re.findall(r"<?\s*\d+\s*%|<5%", rain_line)
                # Parse wind speeds
                winds = re.findall(r"(\d+)mph", wind_line)

                if temps and len(temps) == len(times):
                    for j, time in enumerate(times):
                        hourly.append({
                            "time": time,
                            "temp_c": int(temps[j]) if j < len(temps) else None,
                            "condition": conds[j].strip() if j < len(conds) else "Unknown",
                            "rain_chance": rains[j].replace(" ", "") if j < len(rains) else None,
                            "wind_mph": int(winds[j]) if j < len(winds) else None,
                        })
                    break  # Got first valid hourly block (today)
            except Exception:
                pass
        i += 1

    return hourly


# ─────────────────────────────────────────────
#  PARSE DETAILED SECTION (wind, humidity etc)
# ─────────────────────────────────────────────

def parse_detailed(soup: BeautifulSoup) -> dict:
    """
    Extracts from the detailed section:
    wind_gust, feels_like, humidity, uv, visibility,
    air_pollution, pollen, sunrise, sunset
    """
    text = soup.get_text(separator="\n")
    lines = [l.strip() for l in text.split("\n") if l.strip()]
    full_text = " ".join(lines)

    def find_after(keyword, pattern, text=full_text):
        idx = text.lower().find(keyword.lower())
        if idx == -1:
            return None
        snippet = text[idx:idx+200]
        m = re.search(pattern, snippet)
        return m.group(1) if m else None

    # Wind gust
    wind_gust = find_after("Daily highest gust", r"(\d+)mph")

    # Feels like
    feels_high = find_after("Feels like", r"Daily high\s*(\d+)")
    feels_low  = find_after("Feels like", r"Daily low\s*(\d+)")

    # Humidity
    hum_high = find_after("Humidity", r"Daily high\s*(\d+)")
    hum_low  = find_after("Humidity", r"Daily low\s*(\d+)")

    # UV
    uv = find_after("UV", r"Daily highest level\s*(\w[\w\s]*?)(?:Seek|No risk|You can|Take care|Spend|Avoid)")
    if uv:
        uv = uv.strip()

    # Visibility
    vis_high = find_after("Visibility", r"Daily high\s*([\d.]+km)")
    vis_low  = find_after("Visibility", r"Daily low\s*([\d.]+km)")

    # Air pollution
    pollution = find_after("Air pollution", r"Daily level\s*(\w+)")

    # Pollen
    pollen = find_after("Pollen", r"Daily level\s*([\w\s]+?)(?:\n|Very High grass|High weed|Low|Moderate)")
    if pollen:
        pollen = pollen.strip()

    # Sunrise / Sunset  (format: 04:43 21:19)
    sun_match = re.search(r"(\d{2}:\d{2})\s+(\d{2}:\d{2})", full_text)
    sunrise = sun_match.group(1) if sun_match else None
    sunset  = sun_match.group(2) if sun_match else None

    # Last updated
    updated_match = re.search(r"Updated:\s*(.+?)(?:\n|$)", full_text, re.IGNORECASE)
    last_updated = updated_match.group(1).strip() if updated_match else None

    return {
        "wind_gust_mph":    int(wind_gust) if wind_gust else None,
        "feels_like_high":  int(feels_high) if feels_high else None,
        "feels_like_low":   int(feels_low) if feels_low else None,
        "humidity_high_pct": int(hum_high) if hum_high else None,
        "humidity_low_pct":  int(hum_low) if hum_low else None,
        "uv_level":          uv,
        "visibility_high":   vis_high,
        "visibility_low":    vis_low,
        "air_pollution":     pollution,
        "pollen":            pollen,
        "sunrise":           sunrise,
        "sunset":            sunset,
        "source_last_updated": last_updated,
    }


# ─────────────────────────────────────────────
#  PARSE NEXT HOUR (current conditions)
# ─────────────────────────────────────────────

def parse_next_hour(soup: BeautifulSoup) -> dict:
    """Parses the 'Next hour' current conditions block"""
    text = soup.get_text(separator="\n")
    lines = [l.strip() for l in text.split("\n") if l.strip()]

    result = {
        "temp_c": None,
        "condition": None,
        "feels_like": None,
        "rain_chance": None,
        "wind_gust_mph": None,
        "pollen": None,
    }

    try:
        idx = next(i for i, l in enumerate(lines) if "Next hour" in l)
        block = lines[idx:idx+15]
        block_text = " ".join(block)

        temp = re.search(r"(\d+)°C", block_text)
        result["temp_c"] = int(temp.group(1)) if temp else None

        cond = re.search(
            r"(Sunny day|Sunny intervals|Cloudy|Overcast|Clear night|"
            r"Partly cloudy|Drizzle|Rain|Shower|Thunder|Snow|Sleet|Fog|Mist)",
            block_text, re.IGNORECASE
        )
        result["condition"] = cond.group(0) if cond else None

        feels = re.search(r"Feels like (\d+)°", block_text)
        result["feels_like"] = int(feels.group(1)) if feels else None

        rain = re.search(r"Rain\s*(<?\d+%)", block_text)
        result["rain_chance"] = rain.group(1) if rain else None

        gust = re.search(r"(\d+)mph", block_text)
        result["wind_gust_mph"] = int(gust.group(1)) if gust else None

        pollen_match = re.search(r"(Very High|High|Moderate|Low)\s+pollen", block_text, re.IGNORECASE)
        result["pollen"] = pollen_match.group(0) if pollen_match else None

    except StopIteration:
        pass

    return result


# ─────────────────────────────────────────────
#  SCRAPE ONE CITY
# ─────────────────────────────────────────────

def scrape_city(city_name: str, geohash: str) -> dict:
    url = BASE_URL.format(geohash)
    print(f"  Fetching {city_name} ({geohash})...")

    try:
        response = requests.get(url, headers=HEADERS, timeout=15)
        response.raise_for_status()
    except requests.RequestException as e:
        print(f"  ✗ Failed to fetch {city_name}: {e}")
        return {
            "city": city_name,
            "geohash": geohash,
            "error": str(e),
            "scraped_at": datetime.utcnow().isoformat(),
        }

    soup = BeautifulSoup(response.text, "html.parser")
    today_date = datetime.utcnow().strftime("%Y-%m-%d")

    # Parse all components
    daily_cards  = parse_daily_cards(soup)
    detailed     = parse_detailed(soup)
    next_hour    = parse_next_hour(soup)
    hourly_today = parse_hourly(soup, today_date)

    # Split daily cards into today + forecast
    today_card = {}
    forecast_cards = []

    for i, card in enumerate(daily_cards):
        if i == 0 or (card.get("date") == today_date):
            today_card = card
        else:
            forecast_cards.append(card)

    # Build today block
    today_block = {
        "date": today_date,
        "condition":    today_card.get("condition"),
        "temp_max":     today_card.get("temp_max"),
        "temp_min":     today_card.get("temp_min"),
        "feels_like_high":   detailed.get("feels_like_high"),
        "feels_like_low":    detailed.get("feels_like_low"),
        "wind_gust_mph":     detailed.get("wind_gust_mph"),
        "humidity_high_pct": detailed.get("humidity_high_pct"),
        "humidity_low_pct":  detailed.get("humidity_low_pct"),
        "uv_level":          detailed.get("uv_level"),
        "visibility_high":   detailed.get("visibility_high"),
        "visibility_low":    detailed.get("visibility_low"),
        "air_pollution":     detailed.get("air_pollution"),
        "pollen":            detailed.get("pollen"),
        "sunrise":           detailed.get("sunrise"),
        "sunset":            detailed.get("sunset"),
        "current": next_hour,
        "hourly":  hourly_today,
    }

    # Build forecast blocks (remaining days)
    forecast_blocks = []
    for card in forecast_cards:
        forecast_blocks.append({
            "date":      card.get("date"),
            "condition": card.get("condition"),
            "temp_max":  card.get("temp_max"),
            "temp_min":  card.get("temp_min"),
            # detailed per-day fields available if we scrape with ?date= param
            # for now populated from the main page parsing
        })

    return {
        "city":         city_name,
        "geohash":      geohash,
        "source_url":   url,
        "scraped_at":   datetime.utcnow().isoformat() + "Z",
        "source_updated": detailed.get("source_last_updated"),
        "today":        today_block,
        "forecast":     forecast_blocks,
    }


# ─────────────────────────────────────────────
#  SCRAPE ALL + WRITE JSON
# ─────────────────────────────────────────────

def run():
    print(f"\n{'='*50}")
    print(f"  Met Office Scraper — {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}")
    print(f"  Cities: {len(CITIES)}")
    print(f"{'='*50}\n")

    # Create output dirs
    os.makedirs("data/cities", exist_ok=True)

    all_cities = []
    failed = []

    for city_name, geohash in CITIES.items():
        city_slug = city_name.lower().replace(" ", "_")
        data = scrape_city(city_name, geohash)

        if "error" in data:
            failed.append(city_name)
        else:
            print(f"  ✓ {city_name}")

        all_cities.append(data)

        # Write individual city file
        city_path = f"data/cities/{city_slug}.json"
        with open(city_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)

    # Write combined file
    combined = {
        "generated_at": datetime.utcnow().isoformat() + "Z",
        "total_cities": len(all_cities),
        "cities": all_cities,
    }
    with open("data/weather.json", "w", encoding="utf-8") as f:
        json.dump(combined, f, indent=2, ensure_ascii=False)

    print(f"\n{'='*50}")
    print(f"  Done. {len(all_cities) - len(failed)}/{len(all_cities)} cities scraped.")
    if failed:
        print(f"  Failed: {', '.join(failed)}")
    print(f"  Output: data/weather.json + data/cities/*.json")
    print(f"{'='*50}\n")


if __name__ == "__main__":
    run()
