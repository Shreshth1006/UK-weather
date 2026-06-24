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

async def parse_all_hourly(page) -> dict:
    """
    Returns: { "2026-06-23": [ {time, temp_c, rain_chance, wind_mph}, ... ], "2026-06-24": [...], ... }
    Scrapes ALL hourly tables present on the Detailed forecast page in one pass —
    no clicking required, all 7 days' tables are in the DOM simultaneously.
    """
    result = {}

    tables = await page.query_selector_all("table.forecast-table.hourly-table")

    for table in tables:
        date_str = await attr(table, "data-date")
        if not date_str:
            continue

        # Times from thead
        start_td = await table.query_selector("td.starting-time-step")
        times = []
        if start_td:
            times.append(await text(start_td))
        time_cells = await table.query_selector_all("thead td:not(.starting-time-step), thead th:not([scope='row'])")
        for tc in time_cells:
            t = await text(tc)
            if re.match(r"\d{1,2}(am|pm)", t):
                times.append(t)
        if not times:
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
                val = to_int(await text(tc))
                if val is not None:
                    temps.append(val)

        # Rain chances
        rains = []
        rain_row = await table.query_selector("tr.precipitation-chance-row")
        if rain_row:
            rain_cells = await rain_row.query_selector_all("td")
            for rc in rain_cells:
                t = re.sub(r"[^\d<%]", "", await text(rc)).strip()
                rains.append(t if t else None)

        # Wind speeds
        winds = []
        wind_row = await table.query_selector(
            "tr.wind-speed-row, tr.body-s.wind-speed-row, "
            "tr[class*='wind-speed'], tr[class*='windspeed']"
        )
        if not wind_row:
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
                m = re.search(r"(\d+)\s*mph", t, re.IGNORECASE)
                winds.append(int(m.group(1)) if m else to_int(t))

        day_hourly = []
        for i, time in enumerate(times):
            temp_c = temps[i] if i < len(temps) else None
            day_hourly.append({
                "time":        time,
                "temp_c":      temp_c,
                "temp_f":      round(temp_c * 9 / 5 + 32, 1) if temp_c is not None else None,
                "rain_chance": rains[i] if i < len(rains) else None,
                "wind_mph":    winds[i] if i < len(winds) else None,
            })

        result[date_str] = day_hourly

    return result


# ─────────────────────────────────────────────
#  PARSE DETAILED CARDS (wind, humidity, UV etc)
#  HTML: <div class="card wind-card">
#          <p class="card-data heading-xl">24mph</p>
#        </div>
# ─────────────────────────────────────────────

async def parse_all_detailed(page) -> dict:
    """
    Returns: { "2026-06-23": {wind_gust_mph, feels_like_high, ...}, "2026-06-24": {...}, ... }
    The Detailed forecast page repeats one card-block set per day. Each block sits inside
    a container we identify via the 'Complete sun and moon data for ... date=YYYY-MM-DD' link,
    which lets us recover the date for that block's wind/feels-like/humidity/etc cards.
    """
    result = {}

    # Each day's detail section is a sibling block; the sunrise/sunset link contains the date
    # Find all sun-rise-range containers — each belongs to one day's detail block
    sun_links = await page.query_selector_all("a[href*='aa.usno.navy.mil'][href*='date=']")

    # Also grab all wind/feels-like/humidity/etc cards in document order
    wind_cards     = await page.query_selector_all(".card.wind-card")
    feels_cards    = await page.query_selector_all(".card.temperature-card")
    hum_cards      = await page.query_selector_all(".card.humidity-card")
    uv_cards       = await page.query_selector_all(".card.uv-card, .card-uv")
    vis_cards      = await page.query_selector_all(".card.visibility-card")
    pollution_cards = await page.query_selector_all(".card.air-pollution-card")
    pollen_cards   = await page.query_selector_all(".card.pollen-card")
    sun_ranges     = await page.query_selector_all(".sun-rise-range")

    # Extract dates from the sun-and-moon links, in document order
    dates = []
    for link in sun_links:
        href = await attr(link, "href")
        m = re.search(r"date=(\d{4}-\d{2}-\d{2})", href)
        dates.append(m.group(1) if m else None)

    n_days = len(dates)
    if n_days == 0:
        return result

    for i in range(n_days):
        date_str = dates[i]
        if not date_str:
            continue

        # Wind gust
        wind_gust = None
        if i < len(wind_cards):
            wd = await wind_cards[i].query_selector(".card-data")
            wind_gust = to_int(await text(wd))

        # Feels like high/low
        feels_high = feels_low = None
        if i < len(feels_cards):
            fd = await feels_cards[i].query_selector_all(".card-data")
            vals = [to_int(await text(x)) for x in fd]
            if len(vals) >= 1: feels_high = vals[0]
            if len(vals) >= 2: feels_low = vals[1]

        # Humidity high/low
        hum_high = hum_low = None
        if i < len(hum_cards):
            hd = await hum_cards[i].query_selector_all(".card-data")
            vals = [to_int((await text(x)).replace("%", "")) for x in hd]
            if len(vals) >= 1: hum_high = vals[0]
            if len(vals) >= 2: hum_low = vals[1]

        # UV
        uv = None
        if i < len(uv_cards):
            ud = await uv_cards[i].query_selector(".card-data")
            uv = await text(ud)

        # Visibility high/low
        vis_high = vis_low = None
        if i < len(vis_cards):
            vd = await vis_cards[i].query_selector_all(".card-data")
            km_vals = [await text(x) for x in vd if "km" in (await text(x)).lower()]
            if len(km_vals) >= 1: vis_high = km_vals[0]
            if len(km_vals) >= 2: vis_low = km_vals[1]

        # Air pollution
        air_pollution = None
        if i < len(pollution_cards):
            pd = await pollution_cards[i].query_selector(".card-data")
            air_pollution = await text(pd)

        # Pollen
        pollen = None
        if i < len(pollen_cards):
            pld = await pollen_cards[i].query_selector(".card-data")
            pollen = await text(pld)

        # Sunrise / Sunset
        sunrise = sunset = None
        if i < len(sun_ranges):
            time_els = await sun_ranges[i].query_selector_all("time")
            if len(time_els) >= 1: sunrise = await text(time_els[0])
            if len(time_els) >= 2: sunset = await text(time_els[1])

        result[date_str] = {
            "wind_gust_mph":     wind_gust,
            "feels_like_high":   feels_high,
            "feels_like_low":    feels_low,
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

    return result


async def parse_source_updated(page) -> str:
    """Grabs the 'Updated: 12pm on 23 June 2026' timestamp."""
    source_updated = None
    updated_el = await page.query_selector(".updated strong, .updated-time, [class*=updated] strong")
    if updated_el:
        source_updated = await text(updated_el)
    if not source_updated:
        forecast_el = await page.query_selector(".daily-forecast-section, #daily-forecast, main")
        search_text = await forecast_el.inner_text() if forecast_el else ""
        m = re.search(r"Updated:\s*(.+?(?:am|pm).+?\d{4})", search_text, re.IGNORECASE)
        if m:
            source_updated = m.group(1).strip()
    return source_updated


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
#  PARSE WEATHER WARNINGS
#  HTML: <li class="warning amber" data-date-range="2026-06-22T01:00+01:00/2026-06-23T23:59+01:00"
#            data-impact="3" data-likelihood="3">
#          <p class="warning-severity">Amber warning</p>
#          <p class="warning-types">Extreme heat</p>
#          <p class="warning-period"><time>Until Tuesday 11:59pm</time></p>
#        </li>
# ─────────────────────────────────────────────

async def parse_warnings(page) -> list[dict]:
    warnings = []

    warning_els = await page.query_selector_all("ul.warningsAAG li[id^='warning_'], li.warning")

    for el in warning_els:
        # Severity class — amber / yellow / red
        cls = await attr(el, "class")
        severity_level = None
        for level in ("red", "amber", "yellow"):
            if level in cls.lower():
                severity_level = level
                break

        date_range = await attr(el, "data-date-range")
        impact = await attr(el, "data-impact")
        likelihood = await attr(el, "data-likelihood")

        severity_el = await el.query_selector(".warning-severity")
        severity_text = await text(severity_el)

        type_el = await el.query_selector(".warning-types")
        warning_type = await text(type_el)

        period_el = await el.query_selector(".warning-period")
        period_text = await text(period_el)

        # Split date_range "start/end" into two ISO strings
        valid_from = None
        valid_to = None
        if date_range and "/" in date_range:
            parts = date_range.split("/")
            valid_from = parts[0] if len(parts) > 0 else None
            valid_to = parts[1] if len(parts) > 1 else None

        warnings.append({
            "severity":      severity_level,
            "severity_text": severity_text or None,
            "type":          warning_type or None,
            "valid_from":    valid_from,
            "valid_to":      valid_to,
            "period_text":   period_text or None,
            "impact":        to_int(impact),
            "likelihood":    to_int(likelihood),
        })

    return warnings


# ─────────────────────────────────────────────
#  SCRAPE ONE CITY
# ─────────────────────────────────────────────

async def scrape_city(page, city_name: str, geohash: str) -> dict:
    url = BASE_URL.format(geohash)
    print(f"  Fetching {city_name}...")

    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=30000)
        await page.wait_for_selector(".snapshot.next-day", state="attached", timeout=30000)
        # Scroll to bottom to trigger any lazy-loaded sections (Detailed forecast tables)
        await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        await page.wait_for_timeout(800)
        await page.evaluate("window.scrollTo(0, 0)")
        # Detailed forecast tables render lower on the page — wait for them too
        await page.wait_for_selector("table.forecast-table.hourly-table", state="attached", timeout=30000)
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

    daily_cards     = await parse_daily_cards(page)      # quick summary: date/condition/temp_max/temp_min
    hourly_by_date  = await parse_all_hourly(page)        # { date: [ {time,temp_c,temp_f,rain_chance,wind_mph}, ... ] }
    detail_by_date  = await parse_all_detailed(page)      # { date: {wind_gust_mph, feels_like_high, ...} }
    current         = await parse_current(page)           # next-hour snapshot
    warnings        = await parse_warnings(page)           # list of active warnings with valid_from/valid_to
    source_updated  = await parse_source_updated(page)

    # Map daily_cards by date for quick lookup (condition/temp_max/temp_min)
    summary_by_date = {}
    for c in daily_cards:
        d = c.get("date")
        if d and d not in summary_by_date:
            summary_by_date[d] = c

    def warnings_for_date(date_str: str) -> list[dict]:
        """Returns any warnings whose valid_from/valid_to range covers this date."""
        matched = []
        for w in warnings:
            vf, vt = w.get("valid_from"), w.get("valid_to")
            if not vf or not vt:
                continue
            try:
                d_obj = datetime.strptime(date_str, "%Y-%m-%d").date()
                from_date = datetime.fromisoformat(vf).date()
                to_date = datetime.fromisoformat(vt).date()
                if from_date <= d_obj <= to_date:
                    matched.append(w)
            except Exception:
                continue
        return matched

    # Build forecastday[] — union of all dates seen across hourly + detail + summary
    all_dates = sorted(set(hourly_by_date.keys()) | set(detail_by_date.keys()) | set(summary_by_date.keys()))

    forecastday = []
    for date_str in all_dates:
        summary = summary_by_date.get(date_str, {})
        detail  = detail_by_date.get(date_str, {})
        hours   = hourly_by_date.get(date_str, [])

        day_block = {
            "date": date_str,
            "is_today": date_str == today_date,
            "astro": {
                "sunrise": detail.get("sunrise"),
                "sunset":  detail.get("sunset"),
            },
            "day": {
                "maxtemp_c":           summary.get("temp_max"),
                "mintemp_c":           summary.get("temp_min"),
                "condition":           summary.get("condition"),
                "feels_like_high":     detail.get("feels_like_high"),
                "feels_like_low":      detail.get("feels_like_low"),
                "wind_gust_mph":       detail.get("wind_gust_mph"),
                "humidity_high_pct":   detail.get("humidity_high_pct"),
                "humidity_low_pct":    detail.get("humidity_low_pct"),
                "uv_level":            detail.get("uv_level"),
                "visibility_high":     detail.get("visibility_high"),
                "visibility_low":      detail.get("visibility_low"),
                "air_pollution":       detail.get("air_pollution"),
                "pollen":              detail.get("pollen"),
                "daily_chance_of_rain": max(
                    (to_int(h["rain_chance"]) or 0 for h in hours if h.get("rain_chance")),
                    default=None
                ),
            },
            "hour": hours,
            "warnings": warnings_for_date(date_str),
        }
        forecastday.append(day_block)

    return {
        "city":           city_name,
        "geohash":        geohash,
        "source_url":     url,
        "scraped_at":     datetime.now(timezone.utc).isoformat(),
        "source_updated": source_updated,
        "current":        current,
        "active_warnings": warnings,
        "forecastday":    forecastday,
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