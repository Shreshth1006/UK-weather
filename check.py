
import asyncio
from playwright.async_api import async_playwright

async def debug():
    async with async_playwright() as p:
        b = await p.chromium.launch(headless=True)
        page = await b.new_page()
        await page.goto('https://weather.metoffice.gov.uk/forecast/gcpvj0v07', wait_until='domcontentloaded')
        await page.wait_for_selector('.snapshot.next-day')
        cards = await page.query_selector_all('.snapshot.next-day')
        card = cards[0]
        html = await card.inner_html()
        print(html)
        await b.close()

asyncio.run(debug())
