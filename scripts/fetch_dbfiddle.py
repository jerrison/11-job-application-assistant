"""Fetch SQL content from a db-fiddle URL using Playwright."""

import asyncio

from playwright.async_api import async_playwright


async def main():
    url = "https://www.db-fiddle.com/f/sRqKozBHiTZ9rZ8W14D8wS/29"
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()
        await page.goto(url, wait_until="networkidle", timeout=30000)
        await asyncio.sleep(3)

        # Extract CodeMirror editor contents
        js_code = (
            "() => {"
            "  const eds = document.querySelectorAll('.CodeMirror');"
            "  const r = [];"
            "  for (const e of eds) {"
            "    if (e.CodeMirror) r.push(e.CodeMirror.getValue());"
            "  }"
            "  return r;"
            "}"
        )
        sql_content = await page.evaluate(js_code)

        print("=== Extracted SQL from CodeMirror editors ===")
        for i, c in enumerate(sql_content):
            print(f"--- Editor {i} ---")
            print(c)
            print()

        title = await page.title()
        print(f"Page title: {title}")

        await browser.close()


asyncio.run(main())
