"""
Met Office Weather API
======================
Endpoint: /api/weather?cityName=London

Returns weather data for a single city or all cities.
Data is read from data/weather.json (updated by GitHub Actions scraper).
"""

import json
import os
from http.server import BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs


# ── Load JSON data ──────────────────────────────────────────────
def load_data() -> dict:
    """Load the scraped weather JSON from disk."""
    # Path relative to project root
    base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    json_path = os.path.join(base_dir, "data", "weather.json")

    if not os.path.exists(json_path):
        return None

    with open(json_path, "r", encoding="utf-8") as f:
        return json.load(f)


def find_city(data: dict, city_name: str) -> dict | None:
    """Case-insensitive city lookup."""
    for city in data.get("cities", []):
        if city["city"].lower() == city_name.lower():
            return city
    return None


# ── Vercel serverless handler ───────────────────────────────────
class handler(BaseHTTPRequestHandler):

    def do_GET(self):
        parsed = urlparse(self.path)
        params = parse_qs(parsed.query)

        city_name = params.get("cityName", [None])[0]

        # ── Load data
        data = load_data()

        if data is None:
            self._respond(503, {
                "status": "error",
                "message": "Weather data not available yet. Run the scraper first.",
            })
            return

        # ── No cityName → return all cities summary
        if not city_name:
            summary = {
                "status": "ok",
                "generated_at": data.get("generated_at"),
                "total_cities": data.get("total_cities"),
                "available_cities": [c["city"] for c in data.get("cities", [])],
                "usage": "Add ?cityName=London to get data for a specific city",
            }
            self._respond(200, summary)
            return

        # ── City lookup
        city_data = find_city(data, city_name)

        if not city_data:
            available = [c["city"] for c in data.get("cities", [])]
            self._respond(404, {
                "status": "error",
                "message": f"City '{city_name}' not found.",
                "available_cities": available,
            })
            return

        # ── Return city data
        self._respond(200, {
            "status": "ok",
            "data": city_data,
        })

    def _respond(self, status: int, body: dict):
        payload = json.dumps(body, indent=2, ensure_ascii=False).encode("utf-8")

        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Access-Control-Allow-Origin", "*")   # CORS open
        self.send_header("Cache-Control", "public, max-age=900")  # 15min cache
        self.end_headers()
        self.wfile.write(payload)

    # Silence default request logs (optional)
    def log_message(self, format, *args):
        pass
