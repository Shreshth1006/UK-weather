"""
Met Office Weather Scraper — Playwright version
================================================
Uses real browser (Playwright) to scrape CSS classes directly.
Writes:
  data/weather.json          ← all cities combined
  data/cities/{city}.json    ← one per city

To add a city: add one line to CITIES dict below.
"""

import json
import os
import re
import asyncio
from datetime import datetime, timezone
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeout

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


# ─────────────────────────────────────────────
#  HELPERS
# ─────────────────────────────────────────────

async def text(el) -> str:
    """Get stripped inner text of an element, empty string if None."""
    if not el:
        return ""
    return (await el.inner_text()).strip()


async def attr(el, name: str) -> str:
    """Get attribute value of an element, empty string if None."""
    if not el:
        return ""
    val = await el.get_attribute(name)
    return (val or "").strip()


def to_int(s: str):
    """Extract first integer from string, None if not found."""
    m = re.search(r"-?\d+", s)
    return int(m.group()) if m else None


# ─────────────────────────────────────────────
#  PARSE DAILY SNAPSHOT CARDS
#  HTML: <div class="snapshot next-day" data-date="2026-06-16">
#          <span aria-hidden="true"> 26C </span>   ← max temp
#          <div class="snapshot-weather-description">Sunny intervals</div>
#          (min temp is in screen-reader span)
# ─────────────────────────────────────────────

async def parse_daily_cards(page) -> list[dict]:
    cards = []
    today_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    # Real HTML structure (confirmed from DevTools):
    # <li class="day-tab" data-date="2026-06-16">
    #   <div class="weather-day-elements">
    #     <div class="tab-temp temperature-data">
    #       <span class="tab-temp-high">
    #         <span aria-hidden="true"> 24° </span>
    #         <span class="screen-reader-only">Maximum daytime temperature: 24 degrees Celsius;</span>
    #       </span>
    #       <span class="tab-temp-low">
    #         <span aria-hidden="true"> 15° </span>
    #         <span class="screen-reader-only">Minimum nighttime temperature: 15 degrees Celsius;</span>
    #       </span>
    #     </div>
    #   </div>
    # </li>

    day_els = await page.query_selector_all("li.day-tab")

    for el in day_els:
        date_str = await attr(el, "data-date") or today_date

        # Condition — in the weather symbol div aria-label or h3
        h3_el = await el.query_selector("h3.tab-day")
        # condition is in snapshot section, not day-tab — get from snapshot
        # Use snapshot cards for condition, day-tabs for temps
        temp_max = None
        temp_min = None

        # Max temp
        high_el = await el.query_selector(".tab-temp-high span[aria-hidden='true']")
        if high_el:
            temp_max = to_int(await text(high_el))

        # Min temp
        low_el = await el.query_selector(".tab-temp-low span[aria-hidden='true']")
        if low_el:
            temp_min = to_int(await text(low_el))

        cards.append({
            "date":     date_str,
            "temp_max": temp_max,
            "temp_min": temp_min,
        })

    # Get conditions from snapshot cards (still use these for condition text)
    snap_els = await page.query_selector_all(".snapshot.next-day")
    for i, snap in enumerate(snap_els):
        cond_el = await snap.query_selector(".snapshot-weather-description")
        condition = await text(cond_el)
        if i < len(cards):
            cards[i]["condition"] = condition or None

    return cards


# ─────────────────────────────────────────────
#  PARSE HOURLY TABLE (today only)
#  HTML: <table class="forecast-table hourly-table" data-date="2026-06-15">
#          <thead> <td>7am</td> <td>8am</td> ... </thead>
#          <tbody>
#            <tr class="body-s weather-temperature-row"> temps </tr>
#            <tr class="precipitation-chance-row hourly-table"> rain% </tr>
#            <tr class="wind-speed-row"> wind </tr>
#          </tbody>
# ─────────────────────────────────────────────

async def parse_hourly(page) -> list[dict]:
    today_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    # Find today's hourly table specifically
    table = await page.query_selector(
        f'table.forecast-table.hourly-table[data-date="{today_date}"]'
    )
    if not table:
        # fallback — just grab first hourly table
        table = await page.query_selector("table.forecast-table.hourly-table")
    if not table:
        return []

    # Times from thead
    time_cells = await table.query_selector_all("thead td:not(.starting-time-step), thead th:not([scope='row'])")
    # Also try the starting-time-step td which has the first time
    start_td = await table.query_selector("td.starting-time-step")
    times = []
    if start_td:
        times.append(await text(start_td))
    for tc in time_cells:
        t = await text(tc)
        if re.match(r"\d{1,2}(am|pm)", t):
            times.append(t)

    if not times:
        # fallback: all tds in first tr of thead
        header_tds = await table.query_selector_all("thead tr td")
        for td in header_tds:
            t = await text(td)
            if re.match(r"\d{1,2}(am|pm)", t):
                times.append(t)

    # Temperatures
    temps = []
    temp_row = await table.query_selector("tr.body-s.weather-temperature-row, tr.weather-temperature-row")
    if temp_row:
        temp_cells = await temp_row.query_selector_all("td")
        for tc in temp_cells:
            t = await text(tc)
            val = to_int(t)
            if val is not None:
                temps.append(val)

    # Rain chances
    rains = []
    rain_row = await table.query_selector("tr.precipitation-chance-row")
    if rain_row:
        rain_cells = await rain_row.query_selector_all("td")
        for rc in rain_cells:
            t = await text(rc)
            # Clean up — may have icon text mixed in
            t = re.sub(r"[^\d<%]", "", t).strip()
            if t:
                rains.append(t if t.startswith("<") else t)
            else:
                rains.append(None)

    # Wind speeds — try multiple possible class names
    winds = []
    wind_row = await table.query_selector(
        "tr.wind-speed-row, tr.body-s.wind-speed-row, "
        "tr[class*='wind-speed'], tr[class*='windspeed']"
    )
    if not wind_row:
        # Fallback: find row containing "mph" values
        all_rows = await table.query_selector_all("tbody tr")
        for row in all_rows:
            row_text = await row.inner_text()
            if "mph" in row_text.lower():
                wind_row = row
                break
    if wind_row:
        wind_cells = await wind_row.query_selector_all("td")
        for wc in wind_cells:
            t = await text(wc)
            # Extract just the number before mph
            m = re.search(r"(\d+)\s*mph", t, re.IGNORECASE)
            if m:
                winds.append(int(m.group(1)))
            else:
                val = to_int(t)
                winds.append(val)

    # Zip together
    hourly = []
    for i, time in enumerate(times):
        hourly.append({
            "time":        time,
            "temp_c":      temps[i] if i < len(temps) else None,
            "rain_chance": rains[i] if i < len(rains) else None,
            "wind_mph":    winds[i] if i < len(winds) else None,
        })

    return hourly


# ─────────────────────────────────────────────
#  PARSE DETAILED CARDS (wind, humidity, UV etc)
#  HTML: <div class="card wind-card">
#          <p class="card-data heading-xl">24mph</p>
#        </div>
# ─────────────────────────────────────────────

async def parse_detailed(page) -> dict:

    async def card_value(selector: str) -> str:
        el = await page.query_selector(selector)
        return await text(el)

    async def card_values(selector: str) -> list[str]:
        els = await page.query_selector_all(selector)
        return [await text(e) for e in els]

    # Wind gust
    wind_el = await page.query_selector(".card.wind-card .card-data")
    wind_gust = to_int(await text(wind_el))

    # Feels like — two .card-data values inside temperature-card
    feels_card = await page.query_selector(".card.temperature-card")
    feels_vals = []
    if feels_card:
        feels_data = await feels_card.query_selector_all(".card-data")
        for fd in feels_data:
            feels_vals.append(to_int(await text(fd)))
    feels_high = feels_vals[0] if len(feels_vals) > 0 else None
    feels_low  = feels_vals[1] if len(feels_vals) > 1 else None

    # Humidity — two values inside humidity-card
    hum_card = await page.query_selector(".card.humidity-card")
    hum_vals = []
    if hum_card:
        hum_data = await hum_card.query_selector_all(".card-data")
        for hd in hum_data:
            v = await text(hd)
            hum_vals.append(to_int(v.replace("%", "")))
    hum_high = hum_vals[0] if len(hum_vals) > 0 else None
    hum_low  = hum_vals[1] if len(hum_vals) > 1 else None

    # UV
    uv_card = await page.query_selector(".card.uv-card .card-data, .card-uv .card-data")
    uv = await text(uv_card)

    # Visibility — km values are in .card-data, labels in .card-description
    # Structure: Daily high [Xkm] [label] | Daily low [Ykm] [label]
    vis_card = await page.query_selector(".card.visibility-card")
    vis_high = None
    vis_low  = None
    if vis_card:
        vis_data = await vis_card.query_selector_all(".card-data")
        km_vals = []
        for vd in vis_data:
            t = await text(vd)
            if "km" in t.lower():
                km_vals.append(t)
        if len(km_vals) >= 1:
            vis_high = km_vals[0]
        if len(km_vals) >= 2:
            vis_low = km_vals[1]

    # Air pollution
    poll_card = await page.query_selector(".card.air-pollution-card .card-data")
    air_pollution = await text(poll_card)

    # Pollen
    pollen_card = await page.query_selector(".card.pollen-card .card-data")
    pollen = await text(pollen_card)

    # Sunrise / Sunset — <time datetime="2026-06-15T04:43:00+01:00">04:43</time>
    time_els = await page.query_selector_all(".sun-rise-range time")
    sunrise = await text(time_els[0]) if len(time_els) > 0 else None
    sunset  = await text(time_els[1]) if len(time_els) > 1 else None

    # Source last updated — try direct selector first, then regex fallback
    source_updated = None
    updated_el = await page.query_selector(".updated strong, .updated-time, [class*=updated] strong")
    if updated_el:
        source_updated = await text(updated_el)
    if not source_updated:
        # Regex on just the forecast section text (faster than full body)
        forecast_el = await page.query_selector(".daily-forecast-section, #daily-forecast, main")
        search_text = await forecast_el.inner_text() if forecast_el else ""
        m = re.search(r"Updated:\s*(.+?(?:am|pm).+?\d{4})", search_text, re.IGNORECASE)
        if m:
            source_updated = m.group(1).strip()

    return {
        "source_updated":   source_updated,
        "wind_gust_mph":    wind_gust,
        "feels_like_high":  feels_high,
        "feels_like_low":   feels_low,
        "humidity_high_pct": hum_high,
        "humidity_low_pct":  hum_low,
        "uv_level":          uv or None,
        "visibility_high":   vis_high or None,
        "visibility_low":    vis_low or None,
        "air_pollution":     air_pollution or None,
        "pollen":            pollen or None,
        "sunrise":           sunrise,
        "sunset":            sunset,
    }


# ─────────────────────────────────────────────
#  PARSE CURRENT CONDITIONS (Next Hour block)
#  HTML: <div class="snapshot next-hour active">
#          <h2>Next hour</h2>
#          <span aria-hidden="true">14°C</span>
#          <div class="heading-l">Cloudy</div>
#          <ul class="snapshot-list">
#            <li>Feels like 13°</li>
#            <li>Rain 50%</li>
#            <li>Max gust 16mph from the east</li>
#            <li>Very High pollen</li>
#          </ul>
# ─────────────────────────────────────────────

async def parse_current(page) -> dict:
    result = {
        "temp_c":        None,
        "condition":     None,
        "feels_like":    None,
        "rain_chance":   None,
        "wind_gust_mph": None,
        "pollen":        None,
    }

    el = await page.query_selector(".snapshot.next-hour")
    if not el:
        return result

    # Temp
    temp_el = await el.query_selector("span[aria-hidden='true']")
    result["temp_c"] = to_int(await text(temp_el))

    # Condition
    cond_el = await el.query_selector(".heading-l, .snapshot-weather-description")
    result["condition"] = await text(cond_el) or None

    # Snapshot list items
    list_items = await el.query_selector_all(".snapshot-list li, li")
    for li in list_items:
        t = await text(li)
        if "Feels like" in t:
            result["feels_like"] = to_int(t)
        elif "Rain" in t:
            m = re.search(r"(<?\d+%|<5%)", t)
            result["rain_chance"] = m.group(1) if m else None
        elif "gust" in t.lower() or "mph" in t.lower():
            result["wind_gust_mph"] = to_int(t)
        elif "pollen" in t.lower():
            result["pollen"] = t

    return result


# ─────────────────────────────────────────────
#  SCRAPE ONE CITY
# ─────────────────────────────────────────────

async def scrape_city(page, city_name: str, geohash: str) -> dict:
    url = BASE_URL.format(geohash)
    print(f"  Fetching {city_name}...")

    try:
        # Load page — don't wait for networkidle (site has background requests)
        await page.goto(url, wait_until="domcontentloaded", timeout=30000)
        # Wait specifically for the element we need
        await page.wait_for_selector(".snapshot.next-day", state="attached", timeout=30000)
    except PlaywrightTimeout:
        print(f"  ✗ Timeout: {city_name}")
        return {
            "city":       city_name,
            "geohash":    geohash,
            "error":      "timeout",
            "scraped_at": datetime.now(timezone.utc).isoformat(),
        }
    except Exception as e:
        print(f"  ✗ Error: {city_name} — {e}")
        return {
            "city":       city_name,
            "geohash":    geohash,
            "error":      str(e),
            "scraped_at": datetime.now(timezone.utc).isoformat(),
        }

    today_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    daily_cards = await parse_daily_cards(page)
    hourly      = await parse_hourly(page)
    detailed    = await parse_detailed(page)
    current     = await parse_current(page)

    # First card = today, rest = forecast
    # Deduplicate by date
    today_card = daily_cards[0] if daily_cards else {}
    seen = set()
    forecast = []
    for c in daily_cards[1:]:
        d = c.get("date")
        if d and d not in seen:
            seen.add(d)
            forecast.append(c)

    today_block = {
        "date":              today_date,
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
        "current":           current,
        "hourly":            hourly,
    }

    return {
        "city":           city_name,
        "geohash":        geohash,
        "source_url":     url,
        "scraped_at":     datetime.now(timezone.utc).isoformat(),
        "source_updated": detailed.get("source_updated"),
        "today":          today_block,
        "forecast":       forecast,
    }


# ─────────────────────────────────────────────
#  MAIN — scrape all cities
# ─────────────────────────────────────────────

async def main():
    print(f"\n{'='*50}")
    print(f"  Met Office Scraper (Playwright)")
    print(f"  {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
    print(f"  Cities: {len(CITIES)}")
    print(f"{'='*50}\n")

    os.makedirs("data/cities", exist_ok=True)

    all_cities = []
    failed = []

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True, args=["--no-sandbox", "--disable-dev-shm-usage"])
        context = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            locale="en-GB",
        )
        page = await context.new_page()

        # Only block analytics/ads to keep JS working
        await page.route("**/{gtm,googletagmanager,doubleclick,googlesyndication}**",
                         lambda route: route.abort())

        for city_name, geohash in CITIES.items():
            data = await scrape_city(page, city_name, geohash)
            city_slug = city_name.lower().replace(" ", "_")

            if "error" in data:
                failed.append(city_name)
            else:
                print(f"  ✓ {city_name}")

            all_cities.append(data)

            with open(f"data/cities/{city_slug}.json", "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)

        await browser.close()

    # Write combined file
    combined = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "total_cities": len(all_cities),
        "cities": all_cities,
    }
    with open("data/weather.json", "w", encoding="utf-8") as f:
        json.dump(combined, f, indent=2, ensure_ascii=False)

    print(f"\n{'='*50}")
    print(f"  Done. {len(all_cities) - len(failed)}/{len(all_cities)} scraped.")
    if failed:
        print(f"  Failed: {', '.join(failed)}")
    print(f"  Output: data/weather.json + data/cities/*.json")
    print(f"{'='*50}\n")


if __name__ == "__main__":
    asyncio.run(main())