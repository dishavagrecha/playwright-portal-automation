import asyncio
from contextlib import asynccontextmanager
import html
from io import BytesIO
import json
from pathlib import Path
import re
from unittest.mock import patch
import zipfile

import pytest

from portal_runner.codes import Challenge, TOTPProvider
from portal_runner.evidence import Redactor, sanitize_html, write_failure
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


def test_failure_html_removes_hidden_tokens_and_account_identifiers():
    raw = '''<html><head><meta name="csrf-token" content="unknown-token"></head><body>
      <script>const secret = "pass-word"</script>
      <h1>Account 987654321 for alice</h1>
      <input type="hidden" name="authenticity_token" value="unknown-token">
      <input type="password" value="pass-word">
      <textarea>private customer note</textarea>
      <a href="/alice?code=123456">Continue</a>
    </body></html>'''
    clean = sanitize_html(raw, Redactor(["alice", "pass-word", "123456"]))
    for value in ("alice", "pass-word", "987654321", "unknown-token",
                  "private customer note", "href=", "<script", "<meta"):
        assert value not in clean
    assert "[REDACTED]" in clean


def test_failure_bundle_is_written_without_known_secrets(tmp_path):
    from playwright.async_api import async_playwright

    async def check():
        async with async_playwright() as p:
            browser = await p.chromium.launch()
            page = await browser.new_page()
            try:
                await page.set_content('''<html><body><h1>Account 987654 for alice</h1>
                    <input type="password" value="pass-word"><p>Search failed</p></body></html>''')
                await write_failure(browser, page, tmp_path, "find_release",
                                    ['accountId: 987654 pass-word'],
                                    [{"key": "missing", "strategy": "role_name", "matches": 0}],
                                    Redactor(["alice", "pass-word"]))
                assert all((tmp_path / name).exists() for name in
                           ("page.html", "screenshot.png", "failure.json"))
                contents = (tmp_path / "page.html").read_text() + (tmp_path / "failure.json").read_text()
                for secret in ("alice", "pass-word", "987654"):
                    assert secret not in contents
                assert json.loads((tmp_path / "failure.json").read_text())["stage"] == "find_release"
            finally:
                await page.close()
                await browser.close()
    asyncio.run(check())


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


def test_mutated_saved_github_form_resolves_offline():
    from playwright.async_api import async_playwright

    saved = Path("fixtures/github-release-form.html").read_text()
    targets = json.loads(Path("fixtures/github-release-targets.json").read_text())
    renamed = re.sub(r'class="[^"]*"', 'class="renamed"', saved)
    title = re.search(r'<input\b[^>]*id="release_name"[^>]*>', renamed)
    assert title is not None
    reordered = renamed.replace(title.group(), "", 1)
    reordered = re.sub(r'(<form\b[^>]*>)', r'\1<div class="added-wrapper">'
                       + title.group() + '</div>', reordered, count=1)
    wrapped = f'<html><body><div><iframe srcdoc="{html.escape(reordered, quote=True)}"></iframe></div></body></html>'

    async def check():
        async with async_playwright() as p:
            browser = await p.chromium.launch()
            try:
                for spec in targets.values():
                    winner = await Resolver().find_in_html(browser, wrapped, Target(**spec))
                    assert winner["frame"] == "iframe"
                    assert winner["strategy"] in {"role_name", "label", "placeholder", "nearby_text"}
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


def test_resolver_waits_for_late_source_link():
    from playwright.async_api import async_playwright

    async def check():
        async with async_playwright() as p:
            browser = await p.chromium.launch()
            page = await browser.new_page()
            try:
                await page.set_content('<main id="assets"></main>')

                async def add_link():
                    await asyncio.sleep(0.2)
                    await page.locator('#assets').evaluate(
                        "el => el.innerHTML = '<a href=\"/archive.zip\">Source code (zip)</a>'")

                task = asyncio.create_task(add_link())
                result = await Resolver().wait_for(page, Target(
                    "source_archive", role="link", name="Source code (zip)", tag="a"),
                    timeout_ms=2000)
                await task
                assert await result.is_visible()
            finally:
                await page.close()
                await browser.close()
    asyncio.run(check())


def test_runner_polls_late_button_and_records_winning_strategy(tmp_path, capsys):
    from playwright.async_api import async_playwright
    from portal_runner.runner import Runner

    data = {"owner": "owner", "repository": "repo", "tag": "v1",
            "target_branch": "main", "title": "title", "description": "body", "publish": True}

    async def check():
        async with async_playwright() as p:
            browser = await p.chromium.launch()
            runner = Runner(browser, data, tmp_path, tmp_path / "state")
            try:
                await runner.new_context()
                await runner.page.set_content("<main></main>")

                async def add_button():
                    await asyncio.sleep(0.3)
                    await runner.page.locator("main").evaluate(
                        "el => el.innerHTML = '<button onclick=\"window.clicked=true\">Continue</button>'")

                task = asyncio.create_task(add_button())
                await runner.click(Target("continue", role="button", name="Continue", tag="button"))
                await task
                assert await runner.page.evaluate("window.clicked === true")
                assert runner.manifest["locator_strategies"]["continue"] == {
                    "strategy": "role_name", "frame": "main"}
            finally:
                await runner.context.close()
                await browser.close()

    asyncio.run(check())
    assert "[locator] continue: role_name (main)" in capsys.readouterr().err


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
            return type("Response", (), {"status": 200})()

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


def test_interrupted_run_records_stage_and_status(tmp_path):
    from playwright.async_api import async_playwright
    from portal_runner.runner import Runner

    data = {"owner": "owner", "repository": "repo", "tag": "v1",
            "target_branch": "main", "title": "title", "description": "body", "publish": True}

    class OfflineRunner(Runner):
        async def session_alive(self):
            return True

        async def release_exists(self):
            return False

        async def create_release(self):
            raise asyncio.CancelledError()

    async def check():
        async with async_playwright() as p:
            browser = await p.chromium.launch()
            try:
                runner = OfflineRunner(browser, data, tmp_path / "output", tmp_path / "state")
                with pytest.raises(asyncio.CancelledError):
                    await runner.run()
                manifest = json.loads((tmp_path / "output" / "manifest.json").read_text())
                assert manifest["status"] == "interrupted"
                assert "create_release" in manifest["stage_timings_ms"]
            finally:
                await browser.close()
    asyncio.run(check())
