"""Failure artifacts with credential and account data removed."""
import html
import json
import re
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import quote, unquote, urlsplit, urlunsplit


class Redactor:
    def __init__(self, secrets: list[str]):
        values = {variant for secret in secrets if secret for variant in
                  (secret, quote(secret, safe=""), html.escape(secret, quote=True))}
        self.secrets = sorted(values, key=len, reverse=True)

    def clean(self, value: str) -> str:
        value = html.unescape(unquote(value))
        value = re.sub(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", "[EMAIL]", value)
        for secret in self.secrets:
            value = value.replace(secret, "[REDACTED]")
        value = re.sub(r"(?i)\b(?:account|user|actor|customer|owner)[-_ ]?(?:id|number)?\s*[:=#]\s*[\w-]+",
                       "[ACCOUNT_ID]", value)
        value = re.sub(r'(?i)(["\'](?:accountId|userId|actorId|customerId|repositoryId)["\']\s*:\s*["\'])[^"\']+',
                       r'\1[ACCOUNT_ID]', value)
        value = re.sub(r"\b\d{4,}\b", "[NUMBER]", value)
        value = re.sub(r"\b(?:gh[opusr]_[A-Za-z0-9_]+|github_pat_[A-Za-z0-9_]+)\b",
                       "[TOKEN]", value)
        value = re.sub(r"\b[A-Za-z0-9_-]{24,}\b", "[TOKEN]", value)
        value = re.sub(r"(?i)\b(password|authenticity_token|token|otp|totp|code|secret)\s*[:=]\s*[^\s\"'<>]+",
                       r"\1=[REDACTED]", value)
        return value


class SafeHTML(HTMLParser):
    """Keep visible structure and labels; discard executable and hidden content."""

    allowed = {"html", "body", "main", "section", "article", "form", "fieldset", "legend",
               "div", "span", "label", "input", "textarea", "button", "select", "option",
               "summary", "details", "a", "p", "h1", "h2", "h3", "h4", "h5", "h6",
               "ul", "ol", "li", "strong", "em", "small", "br", "hr"}
    excluded = {"script", "style", "svg", "iframe", "template", "noscript", "canvas"}
    void = {"area", "base", "br", "col", "embed", "hr", "img", "input",
            "link", "meta", "param", "source", "track", "wbr"}
    safe_attrs = {"role", "aria-label", "placeholder", "title", "type", "open"}
    locator_attrs = {"id", "class", "for"}

    def __init__(self, redactor: Redactor, preserve_locator_attrs: bool = False):
        super().__init__(convert_charrefs=True)
        self.redactor = redactor
        self.preserve_locator_attrs = preserve_locator_attrs
        self.parts: list[str] = []
        self.stack: list[tuple[str, bool]] = []
        self.skip_depth = 0

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        hidden = (tag in self.excluded or "hidden" in attributes
                  or attributes.get("aria-hidden") == "true"
                  or (tag == "input" and attributes.get("type") == "hidden")
                  or "display:none" in (attributes.get("style") or "").replace(" ", "").lower())
        if self.skip_depth or hidden:
            if tag not in self.void:
                self.skip_depth += 1
                self.stack.append((tag, False))
            return
        allowed = tag in self.allowed
        if tag not in self.void:
            self.stack.append((tag, allowed))
        if not allowed:
            return
        kept = []
        for key, value in attrs:
            if key in self.safe_attrs or (self.preserve_locator_attrs and key in self.locator_attrs):
                if value is None:
                    kept.append(f" {key}")
                else:
                    clean = html.escape(self.redactor.clean(value), quote=True)
                    kept.append(f' {key}="{clean}"')
        self.parts.append(f"<{tag}{''.join(kept)}>")
        if tag == "textarea":
            self.parts.append("[REDACTED]")

    def handle_endtag(self, tag):
        if tag in self.void or not self.stack:
            return
        start_tag, allowed = self.stack.pop()
        if self.skip_depth:
            self.skip_depth -= 1
        elif allowed:
            self.parts.append(f"</{start_tag}>")

    def handle_data(self, data):
        if self.skip_depth or any(tag == "textarea" for tag, _ in self.stack):
            return
        self.parts.append(html.escape(self.redactor.clean(data)))

    def get_html(self) -> str:
        return "".join(self.parts)


def sanitize_html(raw_html: str, redactor: Redactor, preserve_locator_attrs: bool = False) -> str:
    parser = SafeHTML(redactor, preserve_locator_attrs)
    parser.feed(raw_html)
    parser.close()
    reduced = "\n".join(line.rstrip() for line in parser.get_html().splitlines())
    return re.sub(r"\n\s*\n+", "\n", reduced) + "\n"


async def write_failure(browser, page, directory: Path, stage: str,
                        console: list[str], attempts: list[dict], redactor: Redactor,
                        error: Exception | None = None, target_key: str | None = None):
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    clean_html = sanitize_html(await page.content(), redactor, preserve_locator_attrs=True)
    (directory / "page.html").write_text(clean_html, encoding="utf-8")
    parts = urlsplit(page.url)
    safe_url = redactor.clean(urlunsplit((parts.scheme, parts.netloc, parts.path, "", "")))
    relevant = ([attempt for attempt in attempts if attempt.get("key") == target_key]
                if target_key else attempts[-30:])
    screenshot_kind = "real_page_masked"
    try:
        mask = [page.locator("input, textarea, select, [contenteditable], img, "
                             "[data-login], [data-user-id], [data-account-id]")]
        patterns = [re.escape(item) for item in redactor.secrets if len(item) >= 3]
        patterns += [r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", r"\b\d{4,}\b"]
        pattern = re.compile("|".join(patterns), re.I)
        mask.append(page.get_by_text(pattern))
        await page.screenshot(path=str(directory / "screenshot.png"),
                              full_page=True, mask=mask, mask_color="#111111")
    except Exception:
        screenshot_kind = "sanitized_html_fallback"
        safe_page = await browser.new_page()
        try:
            await safe_page.set_content(clean_html, wait_until="domcontentloaded")
            await safe_page.screenshot(path=str(directory / "screenshot.png"), full_page=True)
        finally:
            await safe_page.close()
    (directory / "failure.json").write_text(json.dumps({
        "stage": stage, "url": safe_url,
        "error_class": type(error).__name__ if error else None,
        "error_message": redactor.clean(str(error)) if error else None,
        "target_key": target_key,
        "screenshot_kind": screenshot_kind,
        "console": [redactor.clean(item) for item in console],
        "resolver_attempts": relevant,
    }, indent=2), encoding="utf-8")
