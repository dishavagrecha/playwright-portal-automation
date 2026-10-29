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
from portal_runner.errors import AuthenticationError, FinalError, RetryableError
from portal_runner.evidence import Redactor, sanitize_html, write_failure
from portal_runner.locator import ResolutionError, Resolver, Target


def test_totp_rfc_vector():
    # RFC 6238 SHA-1 test secret, truncated to the six digits GitHub uses.
    secret = "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ"
    with patch("time.time", return_value=59):
        assert asyncio.run(TOTPProvider(secret, min_remaining=0).get_code(Challenge())) == "287082"


def test_totp_does_not_reuse_a_window_across_provider_instances(tmp_path):
    clock = [90]
    async def advance(seconds):
        clock[0] += seconds
    with patch("portal_runner.codes.time.time", side_effect=lambda: clock[0]):
        window_file = tmp_path / "last-window"
        first = asyncio.run(TOTPProvider("JBSWY3DPEHPK3PXP", min_remaining=0,
                                          last_window_file=window_file, sleep=advance)
                            .get_code(Challenge()))
        second = asyncio.run(TOTPProvider("JBSWY3DPEHPK3PXP", min_remaining=0,
                                           last_window_file=window_file, sleep=advance)
                             .get_code(Challenge()))
    assert first != second
    assert int(window_file.read_text()) > 3


@pytest.mark.parametrize("status", [429, 502])
def test_retryable_http_does_not_become_a_dead_session(tmp_path, status):
    from portal_runner.runner import Runner
    data = {"owner": "owner", "repository": "repo", "tag": "v1",
            "target_branch": "main", "title": "title", "description": "body", "publish": True}
    runner = Runner(None, data, tmp_path, tmp_path / "state")
    class Page:
        url = "https://github.com/settings/profile"
        async def goto(self, *_args, **_kwargs):
            return type("Response", (), {"status": status})()
    runner.page = Page()
    with pytest.raises(RetryableError, match=str(status)):
        asyncio.run(runner.session_alive())


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
                                    [{"key": "old", "strategy": "label", "matches": 1},
                                     {"key": "missing", "strategy": "role_name", "matches": 0}],
                                    Redactor(["alice", "pass-word"]),
                                    error=ResolutionError("Could not locate missing", "missing"),
                                    target_key="missing")
                assert all((tmp_path / name).exists() for name in
                           ("page.html", "screenshot.png", "failure.json"))
                contents = (tmp_path / "page.html").read_text() + (tmp_path / "failure.json").read_text()
                for secret in ("alice", "pass-word", "987654"):
                    assert secret not in contents
                evidence = json.loads((tmp_path / "failure.json").read_text())
                assert evidence["stage"] == "find_release"
                assert evidence["error_class"] == "ResolutionError"
                assert evidence["target_key"] == "missing"
                assert evidence["screenshot_kind"] == "real_page_masked"
                assert len(evidence["resolver_attempts"]) == 1
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
    renamed = renamed.replace('>Publish release</button>', '>Publish</button>')
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


def test_release_form_uses_direct_url_when_create_link_is_missing(tmp_path):
    from portal_runner.runner import Runner

    data = {"owner": "owner", "repository": "repo", "tag": "v1",
            "target_branch": "main", "title": "title", "description": "body", "publish": True}

    class OfflineRunner(Runner):
        def __init__(self, status):
            super().__init__(None, data, tmp_path, tmp_path / "state")
            self.page = type("Page", (), {"url": ""})()
            self.status = status

        async def visit(self, url):
            self.page.url = url
            self.manifest["pages_visited"].append(url)
            return type("Response", (), {"status": self.status})()

        async def click(self, target):
            raise ResolutionError("Create link is absent", target.key)

    runner = OfflineRunner(200)
    asyncio.run(runner.open_release_form())
    assert runner.manifest["pages_visited"] == [
        "https://github.com/owner/repo",
        "https://github.com/owner/repo/releases",
        "https://github.com/owner/repo/releases/new",
    ]

    with pytest.raises(FinalError, match="Release form could not be loaded"):
        asyncio.run(OfflineRunner(404).open_release_form())


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


def test_button_synonym_and_form_scoped_fallback():
    from playwright.async_api import async_playwright

    async def check():
        async with async_playwright() as p:
            browser = await p.chromium.launch()
            try:
                target = Target("publish_release", role="button", name="Publish release",
                                names=("Publish",), tag="button", input_type="submit", scope="form")
                synonym = await Resolver().find_in_html(browser,
                    '<form><button type="submit">Publish</button></form>', target)
                assert synonym["strategy"] == "role_name"
                scoped = await Resolver().find_in_html(browser,
                    '<button type="submit">Other</button><form><button type="submit">Go</button></form>',
                    target)
                assert scoped["strategy"] == "structure"
            finally:
                await browser.close()
    asyncio.run(check())


def test_mock_portal_evaluation_cycle(tmp_path):
    from playwright.async_api import async_playwright
    from portal_runner.mock_portal import run_mock

    data = json.loads(Path("fixtures/mock.input.json").read_text())
    state = tmp_path / "state.json"

    async def check():
        async with async_playwright() as p:
            browser = await p.chromium.launch()
            try:
                await run_mock(browser, data, tmp_path / "fresh", state)
                fresh = json.loads((tmp_path / "fresh/manifest.json").read_text())
                assert fresh["session"] == "fresh_login"
                assert (tmp_path / "fresh/demo-v1.zip").exists()
                await run_mock(browser, data, tmp_path / "reused", state)
                reused = json.loads((tmp_path / "reused/manifest.json").read_text())
                assert reused["session"] == "reused"
                assert fresh["captured_files"][0]["sha256"] == reused["captured_files"][0]["sha256"]
                state.write_text("corrupt")
                await run_mock(browser, data, tmp_path / "stale", state)
                stale = json.loads((tmp_path / "stale/manifest.json").read_text())
                assert stale["session"] == "stale_login"
                changed = {**data, "tag": "demo-v2", "title": "Different document"}
                await run_mock(browser, changed, tmp_path / "changed", state)
                other = json.loads((tmp_path / "changed/manifest.json").read_text())
                assert other["captured_files"][0]["sha256"] != fresh["captured_files"][0]["sha256"]
                with pytest.raises(ResolutionError):
                    await run_mock(browser, changed, tmp_path / "broken", state, True)
                evidence = json.loads((tmp_path / "broken/failure/failure.json").read_text())
                assert evidence["stage"] == "find_release"
                assert evidence["error_class"] == "ResolutionError"
                assert evidence["screenshot_kind"] == "real_page_masked"
                assert evidence["resolver_attempts"]
                assert all(item["key"] == "deliberately_missing_field"
                           for item in evidence["resolver_attempts"])
            finally:
                await browser.close()
    asyncio.run(check())


def test_mock_login_rejects_password_and_retries_one_wrong_code(tmp_path, monkeypatch):
    from playwright.async_api import async_playwright
    from portal_runner.mock_portal import MockRunner, PASSWORD, SECRET, serve

    data = json.loads(Path("fixtures/mock.input.json").read_text())
    monkeypatch.setenv("GITHUB_USERNAME", "demo")
    monkeypatch.setenv("GITHUB_PASSWORD", "wrong-password")

    async def check():
        async with async_playwright() as p:
            browser = await p.chromium.launch()
            try:
                with serve(data, tmp_path / "db.json") as base:
                    runner = MockRunner(browser, data, tmp_path, tmp_path / "state", base)
                    await runner.new_context()
                    try:
                        with pytest.raises(AuthenticationError, match="password"):
                            await runner.login()
                    finally:
                        await runner.context.close()
                    monkeypatch.setenv("GITHUB_PASSWORD", PASSWORD)
                    class WrongThenRight:
                        attempts = []
                        async def get_code(self, challenge):
                            self.attempts.append(challenge.attempt)
                            correct = await TOTPProvider(SECRET).get_code(challenge)
                            return ("000000" if correct != "000000" else "999999") if challenge.attempt == 0 else correct
                    provider = WrongThenRight()
                    runner = MockRunner(browser, data, tmp_path, tmp_path / "state", base)
                    runner.code_provider = provider
                    await runner.new_context()
                    try:
                        await runner.login()
                        assert provider.attempts == [0, 1]
                        assert (tmp_path / "state").exists()
                    finally:
                        await runner.context.close()
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
        async def release_exists(self):
            return True

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
