"""Failure artifacts with known account and credential strings removed."""
import json
import re
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit


class Redactor:
    def __init__(self, secrets: list[str]):
        self.secrets = sorted({s for s in secrets if s}, key=len, reverse=True)

    def clean(self, value: str) -> str:
        for secret in self.secrets:
            value = value.replace(secret, "[REDACTED]")
        value = re.sub(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", "[EMAIL]", value)
        value = re.sub(r"(?i)(password|authenticity_token|otp|totp|code)([\"'\s:=]+)([^\s\"'<>]+)", r"\1\2[REDACTED]", value)
        return value


async def write_failure(browser, page, directory: Path, stage: str,
                        console: list[str], attempts: list[dict], redactor: Redactor):
    directory.mkdir(parents=True, exist_ok=True)
    raw_html = await page.content()
    clean_html = redactor.clean(raw_html)
    # Remove values from all form controls, including fields with unanticipated names.
    clean_html = re.sub(r'(?i)(<input\b[^>]*?)\svalue\s*=\s*(["\']).*?\2', r'\1 value="[REDACTED]"', clean_html)
    clean_html = re.sub(r'(?is)(<textarea\b[^>]*>).*?(</textarea>)', r'\1[REDACTED]\2', clean_html)
    clean_html = re.sub(r'(?is)<script\b[^>]*>.*?</script>', '', clean_html)
    clean_html = re.sub(r'(?is)<iframe\b[^>]*>.*?</iframe>', '', clean_html)
    clean_html = re.sub(r'(?i)\s(?:src|href)\s*=\s*(["\']).*?\1', '', clean_html)
    (directory / "page.html").write_text(clean_html, encoding="utf-8")
    parts = urlsplit(page.url)
    safe_url = redactor.clean(urlunsplit((parts.scheme, parts.netloc, parts.path, "", "")))
    (directory / "failure.json").write_text(json.dumps({
        "stage": stage, "url": safe_url,
        "console": [redactor.clean(item) for item in console],
        "resolver_attempts": attempts,
    }, indent=2), encoding="utf-8")
    # Render the sanitized HTML in a fresh, isolated page. Never save a raw screenshot.
    safe_page = await browser.new_page()
    try:
        await safe_page.set_content(clean_html, wait_until="domcontentloaded")
        await safe_page.screenshot(path=str(directory / "screenshot.png"), full_page=True)
    finally:
        await safe_page.close()
