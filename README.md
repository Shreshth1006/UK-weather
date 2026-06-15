# Met Office Weather API

Scrapes weather.metoffice.gov.uk for 17 UK cities.
Auto-updates every 3 hours via GitHub Actions.
Served as a live API via Vercel.

---

## API

```
GET https://{your-app}.vercel.app/api/weather?cityName=London
GET https://{your-app}.vercel.app/api/weather?cityName=Manchester
GET https://{your-app}.vercel.app/api/weather              ← lists all cities
```

### Response

```json
{
  "status": "ok",
  "data": {
    "city": "London",
    "geohash": "gcpvj0v07",
    "scraped_at": "2026-06-15T06:00:00Z",
    "today": {
      "date": "2026-06-15",
      "condition": "Sunny intervals",
      "temp_max": 23,
      "temp_min": 14,
      "feels_like_high": 19,
      "feels_like_low": 13,
      "wind_gust_mph": 24,
      "humidity_high_pct": 75,
      "humidity_low_pct": 40,
      "uv_level": "High",
      "visibility_high": "30km",
      "visibility_low": "20km",
      "air_pollution": "Low",
      "pollen": "Very High",
      "sunrise": "04:43",
      "sunset": "21:19",
      "current": { ... },
      "hourly": [ { "time": "7am", "temp_c": 14, "condition": "...", "rain_chance": "30%", "wind_mph": 7 } ]
    },
    "forecast": [
      { "date": "2026-06-16", "condition": "Cloudy", "temp_max": 24, "temp_min": 16 }
    ]
  }
}
```

---

## Project Structure

```
met-weather/
├── scraper.py                     ← scrapes Met Office, writes data/
├── requirements.txt
├── vercel.json                    ← Vercel routing config
├── api/
│   └── weather.py                 ← Vercel serverless API handler
├── data/
│   ├── weather.json               ← all cities combined (auto-generated)
│   └── cities/
│       ├── london.json
│       ├── manchester.json
│       └── ...
└── .github/
    └── workflows/
        └── scrape.yml             ← runs scraper every 3 hours
```

---

## Deploy (one time setup)

### 1. Push repo to GitHub
```bash
git init
git add .
git commit -m "init"
git remote add origin https://github.com/{you}/met-weather.git
git push -u origin main
```

### 2. Run scraper once locally to generate data/
```bash
pip install requests beautifulsoup4
python scraper.py
git add data/
git commit -m "add initial data"
git push
```

### 3. Deploy to Vercel
```bash
npm i -g vercel
vercel --prod
```
Or connect your GitHub repo on vercel.com → Import Project → done.

### 4. Test it
```
https://{your-app}.vercel.app/api/weather?cityName=London
```

---

## Adding a New City

1. Go to weather.metoffice.gov.uk, search the city
2. Copy the geohash from the URL: `/forecast/{geohash}`
3. Add one line to `CITIES` in `scraper.py`:

```python
CITIES = {
    "London": "gcpvj0v07",
    "NewCity": "pasteGeohashHere",  # ← just this
}
```

Push → GitHub Actions picks it up on next run.

---

## Cities Tracked

London, Manchester, Birmingham, Glasgow, Bristol, Leeds, Edinburgh,
Liverpool, Cardiff, Sheffield, Belfast, York, Lewisham, Cambridgeshire,
Nottingham, Oxfordshire, Newcastle
# UK-weather
