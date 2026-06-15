"""
Met Office Weather Scraper → Static JSON API
============================================
Scrapes weather.metoffice.gov.uk for UK cities and writes:
  data/weather.json          ← all cities combined
  data/cities/{city}.json    ← one file per city

To add a new city: just add it to CITIES dict below.
"""

import requests
import json
import os
import re
from bs4 import BeautifulSoup
from datetime import datetime, timezone

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
#  PARSE DAILY FORECAST CARDS
#  Real text pattern from page:
#  "Today Today Sunny day; 23° Maximum daytime temperature: 23 degrees Celsius;
#   13° Minimum nighttime temperature: 13 degrees Celsius;"
#  "Mon 15 Mon 15 Jun Sunny day; 23° Maximum..."
# ─────────────────────────────────────────────

def parse_daily_cards(text: str) -> list[dict]:
    days = []

    # The page uses "Maximum daytime temperature: 23 degrees Celsius"
    # and "Minimum nighttime temperature: 13 degrees Celsius"
    # The ° symbol may be garbled as Â° — so we match on "degrees Celsius" instead

    pattern = re.compile(
        r"(Today|Mon|Tue|Wed|Thu|Fri|Sat|Sun)\s+\d*\s*"       # day label e.g. "Today" or "Mon 15"
        r"(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun)?\s*\d*\s*\w*\s*"    # optional repeated label
        r"([A-Za-z][^;]{2,50});\s*"                            # condition e.g. "Sunny day"
        r"\d+[^\d]+Maximum daytime temperature:\s*(\d+)\s*degrees Celsius;\s*"  # max temp
        r"\d+[^\d]+Minimum nighttime temperature:\s*(\d+)\s*degrees Celsius",   # min temp
        re.IGNORECASE
    )

    # Extract dates from href patterns
    date_pattern = re.compile(r"date=(\d{4}-\d{2}-\d{2})")
    dates = date_pattern.findall(text)

    matches = pattern.findall(text)

    for i, m in enumerate(matches):
        label, condition, temp_max, temp_min = m
        condition = condition.strip().rstrip(";").strip()
        # Remove trailing numbers or day names that may bleed in
        condition = re.sub(r'\s+\d+$', '', condition).strip()
        condition = re.sub(r'\s+(Mon|Tue|Wed|Thu|Fri|Sat|Sun).*$', '', condition, flags=re.IGNORECASE).strip()

        date_str = dates[i] if i < len(dates) else None

        days.append({
            "date":      date_str,
            "day":       label.strip(),
            "condition": condition,
            "temp_max":  int(temp_max),
            "temp_min":  int(temp_min),
        })

    return days


# ─────────────────────────────────────────────
#  PARSE HOURLY FORECAST
#  Real text pattern from page (detailed section):
#  "Time 8am 9am 10am 11am..."
#  "Actual (°C) 13° 14° 15°..."
#  "Chance 10% 10% 20%..."
#  "Speed 6mph 8mph..."
# ─────────────────────────────────────────────

def parse_hourly(text: str) -> list[dict]:
    hourly = []

    # Find the first "Time Xam Yam..." line followed by "Actual (°C)" temps
    # This is in the detailed/Temperature section
    time_block = re.search(
        r"Time\s+((?:\d{1,2}(?:am|pm)\s+){3,})"   # "Time 8am 9am 10am..."
        r"Actual\s*\([^)]*\)\s+"                    # "Actual (°C)"
        r"((?:\d+°\s+){3,})",                       # "13° 14° 15°..."
        text, re.IGNORECASE
    )

    if not time_block:
        # Fallback: find "Time 8am 9am..." and "Chance X% Y%..."
        time_block = re.search(
            r"Time\s+((?:\d{1,2}(?:am|pm)\s+){3,})"
            r"(?:Weather symbols\s+)?"
            r"(?:Time[^\n]+\n)?"
            r".*?Actual[^\n]*\n((?:\d+°?\s+){3,})",
            text, re.IGNORECASE | re.DOTALL
        )

    if not time_block:
        return hourly

    # Extract times
    times_raw = time_block.group(1).strip()
    times = re.findall(r"\d{1,2}(?:am|pm)", times_raw)

    # Extract temps
    temps_raw = time_block.group(2).strip()
    temps = re.findall(r"(\d+)°?", temps_raw)

    if not times or not temps:
        return hourly

    # Find rain chances for today (first "Chance X% Y%..." block)
    rain_block = re.search(
        r"Chance\s+((?:(?:<?\d+%|<5%)\s+){3,})",
        text, re.IGNORECASE
    )
    rains = []
    if rain_block:
        rains = re.findall(r"<?\d+%|<5%", rain_block.group(1))

    # Find wind speeds for today (first "Speed Xmph Ymph..." block)
    wind_block = re.search(
        r"Speed\s+((?:\d+mph\s+[A-Za-z\s]+){3,})",
        text, re.IGNORECASE
    )
    winds = []
    if wind_block:
        winds = re.findall(r"(\d+)mph", wind_block.group(1))

    # Zip together — use min length to avoid index errors
    count = min(len(times), len(temps))
    for j in range(count):
        hourly.append({
            "time":        times[j],
            "temp_c":      int(temps[j]),
            "rain_chance": rains[j] if j < len(rains) else None,
            "wind_mph":    int(winds[j]) if j < len(winds) else None,
        })

    return hourly


# ─────────────────────────────────────────────
#  PARSE DETAILED SECTION
#  Real patterns confirmed from raw text
# ─────────────────────────────────────────────

def parse_detailed(text: str) -> dict:

    def first_match(pattern, default=None):
        m = re.search(pattern, text, re.IGNORECASE)
        return m.group(1).strip() if m else default

    # source_updated — just the timestamp, not the whole blob
    # Pattern: "Updated: 5:59am on 15 June 2026"
    updated = first_match(r"Updated:\s*(\d+:\d+(?:am|pm)\s+on\s+\d+\s+\w+\s+\d{4})")

    # Wind gust — first occurrence of "Daily highest gust Xmph"
    wind_gust = first_match(r"Daily highest gust\s+(\d+)mph")

    # Feels like — "Daily high X°C ... Daily low Y°C" after "Feels like"
    feels_section = re.search(r"Feels like.*?Daily high\s+(\d+).*?Daily low\s+(\d+)", text, re.IGNORECASE | re.DOTALL)
    feels_high = int(feels_section.group(1)) if feels_section else None
    feels_low  = int(feels_section.group(2)) if feels_section else None

    # Humidity — "Daily high X% Daily low Y%" after "Humidity"
    hum_section = re.search(r"Humidity\s+Daily high\s+(\d+)%\s+Daily low\s+(\d+)%", text, re.IGNORECASE)
    hum_high = int(hum_section.group(1)) if hum_section else None
    hum_low  = int(hum_section.group(2)) if hum_section else None

    # UV — "Daily highest level High/Very high/Moderate/Low/Extreme"
    uv = first_match(r"Daily highest level\s+(Very high|Very High|High|Moderate|Low|Extreme|No risk)")

    # Visibility — "Daily high Xkm ... Daily low Ykm"
    vis_section = re.search(r"Visibility\s+Daily high\s+([\d.]+km).*?Daily low\s+([\d.]+km)", text, re.IGNORECASE | re.DOTALL)
    vis_high = vis_section.group(1) if vis_section else None
    vis_low  = vis_section.group(2) if vis_section else None

    # Air pollution — "Daily level Low/Moderate/High/Very high"
    pollution = first_match(r"Air pollution\s+Daily level\s+(Very high|High|Moderate|Low)")

    # Pollen — "Daily level Very High/High/Moderate/Low" after Pollen
    pollen_section = re.search(r"Pollen\s+Daily level\s+(Very High|Very high|High|Moderate|Low)", text, re.IGNORECASE)
    pollen = pollen_section.group(1) if pollen_section else None

    # Sunrise / Sunset — "04:43 21:19"
    sun = re.search(r"(\d{2}:\d{2})\s+(\d{2}:\d{2})", text)
    sunrise = sun.group(1) if sun else None
    sunset  = sun.group(2) if sun else None

    return {
        "source_last_updated": updated,
        "wind_gust_mph":       int(wind_gust) if wind_gust else None,
        "feels_like_high":     feels_high,
        "feels_like_low":      feels_low,
        "humidity_high_pct":   hum_high,
        "humidity_low_pct":    hum_low,
        "uv_level":            uv,
        "visibility_high":     vis_high,
        "visibility_low":      vis_low,
        "air_pollution":       pollution,
        "pollen":              pollen,
        "sunrise":             sunrise,
        "sunset":              sunset,
    }


# ─────────────────────────────────────────────
#  PARSE NEXT HOUR (current conditions)
# ─────────────────────────────────────────────

def parse_next_hour(text: str) -> dict:
    result = {
        "temp_c":       None,
        "condition":    None,
        "feels_like":   None,
        "rain_chance":  None,
        "wind_gust_mph": None,
        "pollen":       None,
    }

    idx = text.find("Next hour")
    if idx == -1:
        return result

    block = text[idx:idx+500]

    temp = re.search(r"(\d+)°C", block)
    result["temp_c"] = int(temp.group(1)) if temp else None

    cond = re.search(
        r"(Sunny day|Sunny intervals|Cloudy|Overcast|Clear night|"
        r"Partly cloudy night|Partly cloudy|Drizzle|Light rain|Heavy rain|"
        r"Rain shower|Rain|Light shower|Heavy shower|Shower|Thunder|Snow|Sleet|Fog|Mist)",
        block, re.IGNORECASE
    )
    result["condition"] = cond.group(0).strip() if cond else None

    feels = re.search(r"Feels like\s+(\d+)°", block)
    result["feels_like"] = int(feels.group(1)) if feels else None

    rain = re.search(r"Rain\s+(<?\d+%|<5%)", block)
    result["rain_chance"] = rain.group(1) if rain else None

    gust = re.search(r"(\d+)mph", block)
    result["wind_gust_mph"] = int(gust.group(1)) if gust else None

    pollen = re.search(r"(Very High|High|Moderate|Low)\s+pollen", block, re.IGNORECASE)
    result["pollen"] = pollen.group(0).strip() if pollen else None

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
        print(f"  ✗ Failed: {e}")
        return {
            "city":       city_name,
            "geohash":    geohash,
            "error":      str(e),
            "scraped_at": datetime.now(timezone.utc).isoformat(),
        }

    soup = BeautifulSoup(response.text, "html.parser")
    text = soup.get_text(separator=" ", strip=True)
    today_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    daily_cards  = parse_daily_cards(text)
    detailed     = parse_detailed(text)
    next_hour    = parse_next_hour(text)
    hourly_today = parse_hourly(text)

    # First card = today, rest = forecast
    # Deduplicate forecast by date (keep first occurrence only)
    today_card = daily_cards[0] if daily_cards else {}
    seen_dates = set()
    forecast_cards = []
    for card in daily_cards[1:]:
        d = card.get("date")
        if d and d not in seen_dates:
            seen_dates.add(d)
            forecast_cards.append(card)

    today_block = {
        "date":              today_date,
        "day":               today_card.get("day", "Today"),
        "condition":         today_card.get("condition"),
        "temp_max":          today_card.get("temp_max"),
        "temp_min":          today_card.get("temp_min"),
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
        "current":           next_hour,
        "hourly":            hourly_today,
    }

    forecast_blocks = [
        {
            "date":      c.get("date"),
            "day":       c.get("day"),
            "condition": c.get("condition"),
            "temp_max":  c.get("temp_max"),
            "temp_min":  c.get("temp_min"),
        }
        for c in forecast_cards
    ]

    return {
        "city":           city_name,
        "geohash":        geohash,
        "source_url":     url,
        "scraped_at":     datetime.now(timezone.utc).isoformat(),
        "source_updated": detailed.get("source_last_updated"),
        "today":          today_block,
        "forecast":       forecast_blocks,
    }


# ─────────────────────────────────────────────
#  SCRAPE ALL + WRITE JSON
# ─────────────────────────────────────────────

def run():
    print(f"\n{'='*50}")
    print(f"  Met Office Scraper — {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
    print(f"  Cities: {len(CITIES)}")
    print(f"{'='*50}\n")

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

        with open(f"data/cities/{city_slug}.json", "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)

    combined = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
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