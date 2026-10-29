import asyncio
from contextlib import asynccontextmanager
from io import BytesIO
import json
from unittest.mock import patch
import zipfile

import pytest

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


def test_input_validation_rejects_extra_fields_and_accepts_branch_path(tmp_path):
    from portal_runner.runner import required_inputs

    data = {"owner": "owner", "repository": "repo", "tag": "v1",
            "target_branch": "release/2026", "title": "title", "description": "body", "publish": True}
    path = tmp_path / "input.json"
    path.write_text(json.dumps(data))
    assert required_inputs(path)["target_branch"] == "release/2026"
    data["password"] = "must not enter manifest"
    path.write_text(json.dumps(data))
    with pytest.raises(ValueError, match="Unexpected input fields"):
        required_inputs(path)


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


def test_link_fallback_ignores_matching_heading():
    from playwright.async_api import async_playwright

    html = '''<h2>Releases</h2><a href="/owner/repo/releases" aria-label="View releases">
      <span>Releases</span></a>'''

    async def check():
        async with async_playwright() as p:
            browser = await p.chromium.launch()
            try:
                result = await Resolver().find_in_html(browser, html, Target(
                    "releases", role="link", name="Releases", tag="a",
                    attributes={"href": "/owner/repo/releases"}))
                assert result["strategy"] == "nearby_text"
            finally:
                await browser.close()
    asyncio.run(check())


def test_semantic_iframe_match_beats_main_page_structure():
    from playwright.async_api import async_playwright

    html = '''<input type="date"><iframe srcdoc="<label for='date'>Order date</label>
      <input id='date' type='date'>"></iframe>'''

    async def check():
        async with async_playwright() as p:
            browser = await p.chromium.launch()
            try:
                result = await Resolver().find_in_html(browser, html, Target(
                    "order_date", label="Order date", tag="input", input_type="date"))
                assert result["frame"] == "iframe"
                assert result["strategy"] == "label"
            finally:
                await browser.close()
    asyncio.run(check())


def test_target_menu_accepts_non_main_branch():
    from playwright.async_api import async_playwright

    async def check():
        async with async_playwright() as p:
            browser = await p.chromium.launch()
            try:
                result = await Resolver().find_in_html(browser,
                    '<details><summary>Other menu</summary></details>'
                    '<details><summary>Target: release-branch</summary></details>', Target(
                        "target_menu", role="button", name_pattern=r"^Target:", tag="summary"))
                assert result["strategy"] == "nearby_text"
            finally:
                await browser.close()
    asyncio.run(check())


def test_protected_page_from_another_account_is_not_reused(tmp_path, monkeypatch):
    from playwright.async_api import async_playwright
    from portal_runner.runner import Runner

    monkeypatch.setenv("GITHUB_USERNAME", "expected-user")
    data = {"owner": "owner", "repository": "repo", "tag": "v1",
            "target_branch": "main", "title": "title", "description": "body", "publish": True}

    class OfflineRunner(Runner):
        async def visit(self, url):
            await self.page.set_content('''<html><head>
                <meta name="user-login" content="other-user"></head>
                <body class="logged-in"><h1>Public profile</h1></body></html>''')

    async def check():
        async with async_playwright() as p:
            browser = await p.chromium.launch()
            try:
                runner = OfflineRunner(browser, data, tmp_path, tmp_path / "state")
                await runner.new_context()
                try:
                    assert not await runner.session_alive()
                finally:
                    await runner.context.close()
            finally:
                await browser.close()
    asyncio.run(check())


def test_corrupt_saved_state_is_classified_as_stale(tmp_path):
    from playwright.async_api import async_playwright
    from portal_runner.runner import Runner

    state = tmp_path / "state.json"
    state.write_text("not JSON")
    data = {"owner": "owner", "repository": "repo", "tag": "tag",
            "target_branch": "main", "title": "title", "description": "body", "publish": True}

    class OfflineRunner(Runner):
        async def session_alive(self): return False
        async def login(self): pass
        async def release_exists(self): return True
        async def download(self): pass

    async def check():
        async with async_playwright() as p:
            browser = await p.chromium.launch()
            try:
                runner = OfflineRunner(browser, data, tmp_path / "output", state)
                await runner.run()
                assert runner.manifest["session"] == "stale_login"
            finally:
                await browser.close()
    asyncio.run(check())


def test_download_validates_zip_before_replacing_existing_file(tmp_path):
    from portal_runner.runner import Runner

    class Download:
        def __init__(self, payload):
            self.payload = payload

        async def save_as(self, path):
            from pathlib import Path
            Path(path).write_bytes(self.payload)

    class Page:
        def __init__(self, payload):
            self.payload = payload

        @asynccontextmanager
        async def expect_download(self):
            yield type("Info", (), {"value": asyncio.sleep(0, result=Download(self.payload))})()

    class OfflineRunner(Runner):
        async def click(self, target):
            pass

    data = {"owner": "owner", "repository": "repo", "tag": "v1",
            "target_branch": "main", "title": "title", "description": "body", "publish": True}
    runner = OfflineRunner(None, data, tmp_path, tmp_path / "state")
    destination = tmp_path / "v1.zip"
    destination.write_bytes(b"previous document")
    runner.page = Page(b"not a ZIP")
    with pytest.raises(OSError, match="not a ZIP"):
        asyncio.run(runner.download())
    assert destination.read_bytes() == b"previous document"
    assert not list(tmp_path.glob(".source-*"))

    archive = BytesIO()
    with zipfile.ZipFile(archive, "w") as stream:
        stream.writestr("repo/README.md", "hello")
    runner.page = Page(archive.getvalue())
    asyncio.run(runner.download())
    assert destination.read_bytes() == archive.getvalue()
    assert runner.manifest["captured_files"][0]["bytes"] == len(archive.getvalue())
