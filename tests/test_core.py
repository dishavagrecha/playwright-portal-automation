import asyncio
import os
from unittest.mock import patch

from portal_runner.codes import Challenge, TOTPProvider
from portal_runner.evidence import Redactor
from portal_runner.locator import Resolver, Target


def test_totp_rfc_vector():
    # RFC 6238 SHA-1 test secret, truncated to the six digits GitHub uses.
    secret = "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ"
    with patch("time.time", return_value=59):
        assert asyncio.run(TOTPProvider(secret).get_code(Challenge())) == "287082"


def test_redaction():
    clean = Redactor(["alice", "my-password", "123456"]).clean(
        "alice alice@example.com my-password 123456")
    for value in ("alice", "alice@example.com", "my-password", "123456"):
        assert value not in clean


def test_locator_mutations_offline():
    from playwright.async_api import async_playwright

    html = '''<html><body><iframe srcdoc="
      <form><div class='renamed'><div><label for='order-date'>Order date</label>
      <input id='order-date' class='new-class' type='date'></div></div>
      <div><button>Search</button></div></form>
    "></iframe></body></html>'''

    async def check():
        async with async_playwright() as p:
            browser = await p.chromium.launch()
            try:
                resolver = Resolver()
                found = await resolver.find_in_html(browser, html, Target(
                    "order_date", label="Order date", nearby="Order date",
                    tag="input", input_type="date", class_hint="old-class"))
                assert found["frame"] == "iframe"
                assert found["strategy"] == "label"
            finally:
                await browser.close()
    asyncio.run(check())
