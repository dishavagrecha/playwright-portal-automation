"""Semantic locator resolution used by live pages and saved HTML fixtures."""
from dataclasses import dataclass, field
from typing import Any


@dataclass
class Target:
    key: str
    role: str | None = None
    name: str | None = None
    label: str | None = None
    placeholder: str | None = None
    nearby: str | None = None
    tag: str = "input"
    input_type: str | None = None
    attributes: dict[str, str] = field(default_factory=dict)
    id_hint: str | None = None
    class_hint: str | None = None


class ResolutionError(Exception):
    pass


class Resolver:
    def __init__(self):
        self.attempts: list[dict[str, Any]] = []

    async def find(self, page, target: Target):
        frames = [page.main_frame, *[f for f in page.frames if f != page.main_frame]]
        for frame in frames:
            frame_name = "main" if frame == page.main_frame else "iframe"
            candidates = []
            if target.role and target.name:
                candidates.append(("role_name", frame.get_by_role(target.role, name=target.name, exact=True)))
            if target.label:
                candidates.append(("label", frame.get_by_label(target.label, exact=True)))
            if target.placeholder:
                candidates.append(("placeholder", frame.get_by_placeholder(target.placeholder, exact=True)))
            if target.nearby:
                # A label or heading may move between wrappers; search an ancestor container.
                near = frame.get_by_text(target.nearby, exact=True)
                candidates.append(("nearby_text", near.locator("xpath=ancestor::*[self::div or self::fieldset or self::label][1]").locator(target.tag)))
            elif target.name and target.role in ("link", "button"):
                clickable_tag = "a" if target.role == "link" else "button"
                candidates.append(("nearby_text", frame.get_by_text(target.name, exact=True)
                                   .locator(f"xpath=ancestor-or-self::{clickable_tag}[1]")))
            selector = target.tag
            if target.input_type:
                selector += f'[type="{target.input_type}"]'
            for key, value in target.attributes.items():
                selector += f'[{key}="{value}"]'
            candidates.append(("structure", frame.locator(selector)))
            if target.id_hint:
                candidates.append(("id", frame.locator(f'#{target.id_hint}')))
            if target.class_hint:
                candidates.append(("class", frame.locator(f'.{target.class_hint}')))
            for strategy, locator in candidates:
                try:
                    count = await locator.count()
                    visible = [i for i in range(min(count, 20)) if await locator.nth(i).is_visible()]
                    self.attempts.append({"key": target.key, "frame": frame_name,
                                          "strategy": strategy, "matches": len(visible)})
                    if len(visible) == 1:
                        self.attempts[-1]["won"] = True
                        return locator.nth(visible[0])
                except Exception as exc:
                    self.attempts.append({"key": target.key, "frame": frame_name,
                                          "strategy": strategy, "error": type(exc).__name__})
        raise ResolutionError(f"Could not uniquely locate {target.key}")

    async def find_in_html(self, browser, html: str, target: Target):
        page = await browser.new_page()
        try:
            await page.set_content(html)
            for frame in page.frames:
                if frame != page.main_frame:
                    await frame.wait_for_load_state()
            await self.find(page, target)
            return next(a for a in reversed(self.attempts) if a.get("won"))
        finally:
            # Caller must use find(page, ...) if it needs to interact with the locator.
            await page.close()
