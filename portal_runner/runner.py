import argparse
import asyncio
import hashlib
import json
import os
import time
from pathlib import Path

from playwright.async_api import async_playwright, Error as PlaywrightError, TimeoutError as PlaywrightTimeout

from .codes import Challenge, TOTPProvider
from .evidence import Redactor, write_failure
from .locator import ResolutionError, Resolver, Target


def required_inputs(path: Path) -> dict:
    data = json.loads(path.read_text(encoding="utf-8"))
    for key in ("owner", "repository", "tag", "target_branch", "title", "description"):
        if not isinstance(data.get(key), str) or not data[key]:
            raise ValueError(f"Input {key} must be a nonempty string")
    if data.get("publish") is not True:
        raise ValueError("Set publish=true in the input to authorize a release in your repository")
    for key in ("owner", "repository", "tag", "target_branch"):
        if not all(c.isalnum() or c in "._-" for c in data[key]):
            raise ValueError(f"Unsafe characters in {key}")
    return data


class Runner:
    def __init__(self, browser, data: dict, output: Path, state: Path):
        self.browser, self.data, self.output, self.state = browser, data, output, state
        self.resolver = Resolver()
        self.console: list[str] = []
        self.manifest = {"inputs": data, "pages_visited": [], "stage_timings_ms": {},
                         "captured_files": [], "retry_events": [], "session": None,
                         "status": "running"}
        self.stage = "startup"
        secrets = [os.getenv("GITHUB_USERNAME", ""), os.getenv("GITHUB_PASSWORD", ""),
                   os.getenv("GITHUB_TOTP_SECRET", ""), data["owner"], data["repository"]]
        self.redactor = Redactor(secrets)

    async def step(self, key, operation, retry=False):
        self.stage = key
        start = time.monotonic()
        last = None
        for attempt in range(3 if retry else 1):
            try:
                result = await operation()
                self.manifest["stage_timings_ms"][key] = round((time.monotonic() - start) * 1000)
                return result
            except (PlaywrightTimeout, OSError) as exc:
                last = exc
                self.manifest["retry_events"].append({"stage": key, "attempt": attempt + 1,
                                                       "error": type(exc).__name__,
                                                       "will_retry": retry and attempt < 2})
                if attempt < (2 if retry else 0):
                    await asyncio.sleep(0.5 * 2 ** attempt)
        self.manifest["stage_timings_ms"][key] = round((time.monotonic() - start) * 1000)
        raise last

    async def visit(self, url):
        await self.page.goto(url, wait_until="domcontentloaded")
        self.manifest["pages_visited"].append(self.page.url)

    async def click(self, target):
        await (await self.resolver.find(self.page, target)).click()

    async def fill(self, target, value):
        await (await self.resolver.find(self.page, target)).fill(value)

    async def session_alive(self):
        # Positive authenticated marker plus a protected settings page, not only URL.
        await self.visit("https://github.com/settings/profile")
        heading = self.page.get_by_role("heading", name="Public profile")
        avatar = self.page.locator('summary[aria-label*="profile"], button[aria-label*="profile"]')
        return await heading.count() > 0 and await avatar.count() > 0

    async def login(self):
        await self.visit("https://github.com/login")
        await self.fill(Target("username", label="Username or email address", tag="input", id_hint="login_field"), os.environ["GITHUB_USERNAME"])
        await self.fill(Target("password", label="Password", tag="input", input_type="password", id_hint="password"), os.environ["GITHUB_PASSWORD"])
        await self.click(Target("sign_in", role="button", name="Sign in", tag="input", input_type="submit"))
        await self.page.wait_for_load_state("domcontentloaded")
        provider = TOTPProvider()
        for attempt in range(2):
            code = await provider.get_code(Challenge(attempt))
            self.redactor.secrets.insert(0, code)
            await self.fill(Target("two_factor_code", label="Authentication code", tag="input", input_type="text", id_hint="app_totp"), code)
            # GitHub's current six-digit field can submit itself on the last digit.
            # Wait for that navigation before looking for an explicit submit button.
            try:
                await self.page.wait_for_url(lambda url: "/sessions/two-factor" not in url,
                                             timeout=5000)
            except PlaywrightTimeout:
                pass
            if "/sessions/two-factor" in self.page.url:
                try:
                    await self.click(Target("verify_code", role="button", name="Verify", tag="button", input_type="submit"))
                except ResolutionError:
                    # No submit control: the code was auto-submitted and rejected.
                    pass
            await self.page.wait_for_load_state("domcontentloaded")
            if "/sessions/two-factor" not in self.page.url and await self.session_alive():
                self.state.parent.mkdir(parents=True, exist_ok=True)
                await self.context.storage_state(path=str(self.state))
                os.chmod(self.state, 0o600)
                return
        raise RuntimeError("Second factor rejected twice or authentication did not complete")

    async def open_release_form(self):
        d = self.data
        await self.visit(f'https://github.com/{d["owner"]}/{d["repository"]}')
        await self.click(Target("releases", role="link", name="Releases", tag="a", attributes={"href": f'/{d["owner"]}/{d["repository"]}/releases'}))
        await self.page.wait_for_url(f'**/{d["owner"]}/{d["repository"]}/releases')
        self.manifest["pages_visited"].append(self.page.url)
        await self.click(Target("new_release", role="link", name="Draft a new release", tag="a"))
        await self.page.wait_for_url(f'**/{d["owner"]}/{d["repository"]}/releases/new')
        self.manifest["pages_visited"].append(self.page.url)

    async def release_exists(self):
        d = self.data
        await self.visit(f'https://github.com/{d["owner"]}/{d["repository"]}/releases/tag/{d["tag"]}')
        return (await self.page.get_by_role("link", name="Source code (zip)").count()) > 0

    async def create_release(self):
        d = self.data
        await self.open_release_form()
        await self.click(Target("tag_menu", role="button", name="Choose a tag", tag="button"))
        await self.fill(Target("tag", role="textbox", name="Find or create a new tag", placeholder="Find or create a new tag", tag="input", input_type="text"), d["tag"])
        await self.click(Target("create_tag", role="button", name=f'Create new tag: {d["tag"]}', tag="button"))
        # The target branch choice is available after creating a new tag.
        await self.click(Target("target_menu", role="button", name="Target", tag="button"))
        await self.click(Target("branch", role="option", name=d["target_branch"], tag="button"))
        await self.fill(Target("release_title", label="Release title", tag="input", id_hint="release_name"), d["title"])
        await self.fill(Target("release_description", label="Describe this release", placeholder="Describe this release", tag="textarea", id_hint="release_body"), d["description"])
        await self.click(Target("publish_release", role="button", name="Publish release", tag="button", input_type="submit"))
        await self.page.wait_for_load_state("domcontentloaded")
        if not await self.release_exists():
            raise RuntimeError("Release was not visible after publishing")

    async def download(self):
        target = Target("source_archive", role="link", name="Source code (zip)", tag="a")
        async with self.page.expect_download() as info:
            await self.click(target)
        download = await info.value
        filename = f'{self.data["tag"]}.zip'
        destination = self.output / filename
        await download.save_as(str(destination))
        digest = hashlib.sha256(destination.read_bytes()).hexdigest()
        self.manifest["captured_files"].append({"path": str(destination), "sha256": digest,
                                                  "bytes": destination.stat().st_size})

    async def run(self):
        self.output.mkdir(parents=True, exist_ok=True)
        state_valid = False
        if self.state.exists():
            try:
                saved = json.loads(self.state.read_text(encoding="utf-8"))
                state_valid = isinstance(saved.get("cookies"), list) and isinstance(saved.get("origins"), list)
            except (OSError, ValueError):
                pass
        try:
            self.context = await self.browser.new_context(
                accept_downloads=True, storage_state=str(self.state) if state_valid else None)
        except (ValueError, TypeError, PlaywrightError):
            # A parseable but invalid state must behave like a missing state.
            state_valid = False
            self.context = await self.browser.new_context(accept_downloads=True)
        self.context.set_default_timeout(10000)
        self.page = await self.context.new_page()
        self.page.on("console", lambda msg: self.console.append(msg.text[:500]))
        try:
            alive = await self.step("session_check", self.session_alive, retry=True)
            if not alive:
                self.manifest["session"] = "fresh_login" if not state_valid else "stale_login"
                await self.step("login", self.login)
            else:
                self.manifest["session"] = "reused"
            exists = await self.step("find_release", self.release_exists, retry=True)
            if not exists:
                await self.step("create_release", self.create_release)
            await self.step("download", self.download, retry=True)
            self.manifest["status"] = "success"
        except Exception as exc:
            self.manifest["status"] = "failure"
            self.manifest["error"] = type(exc).__name__
            await write_failure(self.browser, self.page, self.output / "failure", self.stage,
                                self.console, self.resolver.attempts, self.redactor)
            raise
        finally:
            (self.output / "manifest.json").write_text(json.dumps(self.manifest, indent=2), encoding="utf-8")
            await self.context.close()


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, default=Path("artifacts/run"))
    parser.add_argument("--state", type=Path, default=Path(".state/github.json"))
    parser.add_argument("--headed", action="store_true")
    args = parser.parse_args()
    data = required_inputs(args.input)
    for name in ("GITHUB_USERNAME", "GITHUB_PASSWORD", "GITHUB_TOTP_SECRET"):
        if not os.getenv(name):
            parser.error(f"Missing {name}")
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=not args.headed)
        try:
            await Runner(browser, data, args.output, args.state).run()
        finally:
            await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
