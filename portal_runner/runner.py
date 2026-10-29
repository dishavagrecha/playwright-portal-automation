import argparse
import asyncio
import hashlib
import json
import os
import re
import tempfile
import time
import zipfile
from pathlib import Path

from playwright.async_api import async_playwright, Error as PlaywrightError, TimeoutError as PlaywrightTimeout

from .codes import Challenge, TOTPProvider
from .evidence import Redactor, write_failure
from .locator import ResolutionError, Resolver, Target


def required_inputs(path: Path) -> dict:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("Input must be a JSON object")
    allowed = {"owner", "repository", "tag", "target_branch", "title", "description", "publish"}
    unknown = set(data) - allowed
    if unknown:
        raise ValueError(f"Unexpected input fields: {', '.join(sorted(unknown))}")
    for key in ("owner", "repository", "tag", "target_branch", "title", "description"):
        if not isinstance(data.get(key), str) or not data[key].strip():
            raise ValueError(f"Input {key} must be a nonempty string")
    if data.get("publish") is not True:
        raise ValueError("Set publish=true in the input to authorize a release in your repository")
    for key in ("owner", "repository", "tag"):
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", data[key]) or ".." in data[key]:
            raise ValueError(f"Unsafe characters in {key}")
    branch = data["target_branch"]
    if (not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]*", branch)
            or ".." in branch or "//" in branch or branch.endswith("/")):
        raise ValueError("Unsafe characters in target_branch")
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
            except Exception:
                self.manifest["stage_timings_ms"][key] = round((time.monotonic() - start) * 1000)
                raise
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
        # Verify both a protected page and the account whose repository we may change.
        await self.visit("https://github.com/settings/profile")
        heading = self.page.get_by_role("heading", name="Public profile")
        user_meta = self.page.locator('meta[name="user-login"][content]')
        logged_in_body = self.page.locator("body.logged-in")
        return (await heading.count() > 0 and await user_meta.count() == 1
                and (await user_meta.get_attribute("content") or "").casefold()
                == os.environ["GITHUB_USERNAME"].casefold()
                and await logged_in_body.count() > 0)

    async def new_context(self, saved_state: Path | None = None):
        self.context = await self.browser.new_context(
            accept_downloads=True, storage_state=str(saved_state) if saved_state else None)
        self.context.set_default_timeout(10000)
        self.page = await self.context.new_page()
        self.page.on("console", lambda msg: self.console.append(msg.text[:500]))

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
                self.state.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                saved = await self.context.storage_state()
                fd, temporary = tempfile.mkstemp(dir=self.state.parent, prefix=".github-state-")
                try:
                    with os.fdopen(fd, "w", encoding="utf-8") as stream:
                        json.dump(saved, stream)
                    os.replace(temporary, self.state)
                finally:
                    if os.path.exists(temporary):
                        os.unlink(temporary)
                return
        raise RuntimeError("Second factor rejected twice or authentication did not complete")

    async def open_release_form(self):
        d = self.data
        await self.visit(f'https://github.com/{d["owner"]}/{d["repository"]}')
        # Empty repositories show Releases as a heading, not a navigable link.
        await self.visit(f'https://github.com/{d["owner"]}/{d["repository"]}/releases')
        await self.click(Target("new_release", role="link", name="Create a new release", tag="a",
                                attributes={"href": f'/{d["owner"]}/{d["repository"]}/releases/new'}))
        await self.page.wait_for_url(f'**/{d["owner"]}/{d["repository"]}/releases/new')
        self.manifest["pages_visited"].append(self.page.url)

    async def release_exists(self):
        d = self.data
        await self.visit(f'https://github.com/{d["owner"]}/{d["repository"]}/releases/tag/{d["tag"]}')
        assets = self.page.locator("summary").filter(has_text="Assets")
        if await assets.count() == 0:
            return False
        if await assets.locator("xpath=..").get_attribute("open") is None:
            await assets.click()
        try:
            await self.page.get_by_role("link", name="Source code (zip)").wait_for(
                state="visible", timeout=15000)
            return True
        except PlaywrightTimeout:
            return False

    async def create_release(self):
        d = self.data
        await self.open_release_form()
        await self.click(Target("tag_menu", role="button", name="Tag: Select tag", tag="button",
                                id_hint="ref-picker-releases-tag"))
        await self.fill(Target("tag_filter", role="textbox", name="Filter tags",
                               placeholder="Search or create a new tag", tag="input", input_type="text"), d["tag"])
        await self.click(Target("create_tag_dialog", role="button", name="Create new tag", tag="button"))
        await self.fill(Target("tag_name", role="textbox", name="Tag name", tag="input", input_type="text"), d["tag"])
        await self.click(Target("confirm_tag", role="button", name="Create", tag="button", input_type="submit"))
        # The target branch determines the commit used for the new tag.
        await self.click(Target("target_menu", role="button", name_pattern=r"^Target:", tag="summary"))
        await self.click(Target("branch", role="menuitemradio", name=d["target_branch"], tag="li"))
        await self.fill(Target("release_title", role="textbox", name="Enter release title",
                               placeholder="Title", tag="input", id_hint="release_name"), d["title"])
        await self.fill(Target("release_description", label="Release description",
                               tag="textarea", id_hint="release_body"), d["description"])
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
        fd, temporary = tempfile.mkstemp(dir=self.output, prefix=".source-", suffix=".zip")
        os.close(fd)
        try:
            await download.save_as(temporary)
            try:
                with zipfile.ZipFile(temporary) as archive:
                    if not archive.namelist() or archive.testzip() is not None:
                        raise OSError("Downloaded archive is empty or damaged")
            except zipfile.BadZipFile as exc:
                raise OSError("Downloaded archive is not a ZIP file") from exc
            os.replace(temporary, destination)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
        with destination.open("rb") as stream:
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
        self.manifest["captured_files"].append({"path": str(destination), "sha256": digest,
                                                  "bytes": destination.stat().st_size})

    async def run(self):
        self.output.mkdir(parents=True, exist_ok=True, mode=0o700)
        state_present = self.state.exists()
        state_valid = False
        if state_present:
            try:
                saved = json.loads(self.state.read_text(encoding="utf-8"))
                state_valid = isinstance(saved.get("cookies"), list) and isinstance(saved.get("origins"), list)
            except (OSError, ValueError):
                pass
        try:
            await self.new_context(self.state if state_valid else None)
        except (ValueError, TypeError, PlaywrightError):
            # A parseable but invalid state must behave like a missing state.
            state_valid = False
            await self.new_context()
        try:
            alive = await self.step("session_check", self.session_alive, retry=True)
            if not alive:
                self.manifest["session"] = "stale_login" if state_present else "fresh_login"
                if state_present:
                    # Expired or wrong-account cookies must not affect the next login.
                    await self.context.close()
                    await self.new_context()
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
            try:
                await write_failure(self.browser, self.page, self.output / "failure", self.stage,
                                    self.console, self.resolver.attempts, self.redactor)
            except Exception as evidence_error:
                self.manifest["evidence_error"] = type(evidence_error).__name__
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
    if data["owner"].casefold() != os.environ["GITHUB_USERNAME"].casefold():
        parser.error("Input owner must match GITHUB_USERNAME for this account-only runner")
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=not args.headed)
        try:
            await Runner(browser, data, args.output, args.state).run()
        finally:
            await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
