"""Run the production resolver against saved HTML, without visiting a portal."""
import argparse
import asyncio
import json
from pathlib import Path

from playwright.async_api import async_playwright

from .locator import Resolver, Target


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--html", type=Path, required=True)
    parser.add_argument("--targets", type=Path, required=True)
    args = parser.parse_args()
    html = args.html.read_text(encoding="utf-8")
    targets = json.loads(args.targets.read_text(encoding="utf-8"))
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        try:
            for key, spec in targets.items():
                resolver = Resolver()
                winner = await resolver.find_in_html(browser, html, Target(**spec))
                print(json.dumps({"target": key, "winner": winner,
                                  "attempts": resolver.attempts}))
        finally:
            await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
