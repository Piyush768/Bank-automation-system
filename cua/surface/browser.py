"""Playwright implementation of the Surface seam (web, incl. legacy web)."""

from __future__ import annotations

import re
import secrets as pysecrets
import time
from typing import Callable

from playwright.sync_api import Error as PWError
from playwright.sync_api import Page, sync_playwright

from cua.safety.policy import Policy
from cua.safety.redact import SCREEN_MASK_PATTERNS, Redactor
from cua.schema.artifact import (
    CoordsLocator,
    CssLocator,
    LabelLocator,
    Locator,
    RoleLocator,
    TableCellLocator,
    Target,
    TextLocator,
)
from cua.surface import dom_scripts as js
from cua.surface.base import Observation, Resolution, Surface, UIElement


class ControlViolation(RuntimeError):
    """Automation tried to act while it does not hold control of the session."""


class BrowserSurface(Surface):
    def __init__(
        self,
        policy: Policy,
        redactor: Redactor,
        headed: bool = False,
        on_blocked: Callable[[str, str], None] | None = None,
        viewport: tuple[int, int] = (1280, 900),
        action_timeout_ms: int = 5000,
    ):
        self.policy = policy
        self.redactor = redactor
        self.action_timeout_ms = action_timeout_ms
        self.blocked: list[dict] = []
        self._on_blocked = on_blocked
        # Set by the ControlChannel: raises if `actor` does not hold control.
        self.control_check: Callable[[str], None] = lambda actor: None
        self.on_human_event: Callable[[dict], None] = lambda ev: None

        self._pw = sync_playwright().start()
        self._browser = self._pw.chromium.launch(headless=not headed)
        self._ctx = self._browser.new_context(viewport={"width": viewport[0], "height": viewport[1]})
        # Guardrail at the network layer: every request, not just agent decisions.
        self._ctx.route("**/*", self._route)
        self._ctx.expose_binding("__cuaHuman", lambda source, ev: self.on_human_event(ev))
        self._ctx.add_init_script(js.HUMAN_CAPTURE)
        self.page: Page = self._ctx.new_page()

    # ------------------------------------------------------------------ infra

    def _route(self, route, request) -> None:
        ok, reason = self.policy.url_allowed(request.url)
        if ok:
            route.continue_()
            return
        self.blocked.append({"url": self.redactor.text(request.url), "reason": reason})
        if self._on_blocked:
            self._on_blocked(request.url, reason)
        route.abort("blockedbyclient")

    def close(self) -> None:
        try:
            self._browser.close()
        finally:
            self._pw.stop()

    def _eval(self, script: str, arg=None, retries: int = 3):
        for i in range(retries):
            try:
                return self.page.evaluate(script, arg)
            except PWError as e:
                # Navigation in flight destroys the JS context; wait and retry.
                if i == retries - 1:
                    raise
                if "context was destroyed" in str(e) or "navigation" in str(e).lower():
                    self.page.wait_for_timeout(200)
                    continue
                raise

    # ------------------------------------------------------------- perception

    def observe(self, max_text: int = 120) -> Observation:
        self.settle()
        raw = self._eval(js.OBSERVE, {"max_text": max_text})
        return Observation(
            url=raw["url"],
            title=raw["title"],
            landmarks=raw["landmarks"],
            elements=[UIElement(**{k: v for k, v in e.items() if k in UIElement.model_fields}) for e in raw["elements"]],
            page_text=raw["page_text"],
            viewport=tuple(raw["viewport"]),
        ) if raw else Observation(url=self.page.url, title="", landmarks=[], elements=[], page_text="")

    def _raw_elements(self) -> dict[int, dict]:
        raw = self._eval(js.OBSERVE, {"max_text": 120})
        return {e["idx"]: e for e in raw["elements"]}

    def page_text(self) -> str:
        try:
            return self._eval("() => (document.body ? document.body.innerText : '')", retries=2) or ""
        except PWError:
            return ""

    def landmarks(self) -> list[str]:
        try:
            return self._eval(js.LANDMARKS, retries=2) or []
        except PWError:
            return []

    def current_url(self) -> str:
        return self.page.url

    def settle(self) -> None:
        try:
            self.page.wait_for_load_state("domcontentloaded", timeout=15000)
        except PWError:
            pass
        try:
            self.page.wait_for_load_state("networkidle", timeout=2500)
        except PWError:
            pass

    def pump(self, ms: int = 100) -> None:
        self.page.wait_for_timeout(ms)

    # -------------------------------------------------------------- discovery

    def act_on_index(self, idx: int, action: str, value: str | None, actor: str = "automation") -> None:
        self.control_check(actor)
        self._do(self.page.locator(f'[data-cua-idx="{idx}"]').first, action, value)

    def read_index(self, idx: int) -> str:
        return self.page.locator(f'[data-cua-idx="{idx}"]').first.inner_text().strip()

    def _validate(self, strategy: Locator, idx: int) -> bool:
        res = self._eval(js.RESOLVE, {"strategy": strategy.model_dump(), "token": "v", "idx": idx})
        return bool(res and res["count"] == 1 and res["same"])

    def describe_target(self, idx: int, obs: Observation, params: dict[str, str]) -> Target:
        """Build ranked locator candidates for element `idx` and keep only those
        that, right now, resolve to exactly this element. Ambiguous candidates
        are dropped at record time rather than discovered broken at replay."""
        el = obs.element(idx)
        raw = self._raw_elements().get(idx, {})
        row_cells: list[str] = raw.get("row_cells", [])
        cell_index: int = raw.get("cell_index", -1)
        param_values = [v for v in params.values() if v]
        cands: list[Locator] = []
        c = el.control

        def row_keys() -> list[str]:
            # Prefer a cell holding a parameter value (e.g. the member number),
            # then descriptive text cells ("PRIMARY SAVINGS"), never the target cell.
            keys = [v for v in param_values if any(v in cell for cell in row_cells)]
            others = [t for i, t in enumerate(row_cells) if i != cell_index and re.search(r"[A-Za-z]{3}", t)]
            others.sort(key=lambda t: -len(re.findall(r"[A-Za-z]", t)))
            return keys + others

        if c in ("input", "select"):
            if el.label:
                cands.append(LabelLocator(label=el.label, relation="right_of", control=c))
            if el.role and el.name:
                cands.append(RoleLocator(role=el.role, name=el.name))
        elif c == "clickable":
            row_has_param = any(v in el.row_text for v in param_values)
            table_first = row_has_param and el.column_header
            if table_first:
                for k in row_keys()[:3]:
                    cands.append(TableCellLocator(row_contains=k, column=el.column_header, control="clickable"))
            # In a data row (it contains the parameter), the control's own text is
            # record data (e.g. the member's name): instance-specific AND PII, so
            # it must not become a locator.
            if el.text and not row_has_param:
                cands.append(TextLocator(text=el.text, control="clickable"))
            if el.role and el.name and not row_has_param:
                cands.append(RoleLocator(role=el.role, name=el.name))
            if not table_first and el.column_header:
                for k in row_keys()[:3]:
                    cands.append(TableCellLocator(row_contains=k, column=el.column_header, control="clickable"))
        else:  # text to read
            if el.column_header:
                for k in row_keys()[:3]:
                    cands.append(TableCellLocator(row_contains=k, column=el.column_header, control="text"))
            if el.label:
                cands.append(LabelLocator(label=el.label, relation="right_of", control="text"))
        if el.css:
            cands.append(CssLocator(selector=el.css, control=c))
        x, y, w, h = el.bbox
        vw, vh = obs.viewport
        cands.append(CoordsLocator(x=round(x + w / 2, 1), y=round(y + h / 2, 1), viewport_w=vw, viewport_h=vh, control=c))

        kept: list[Locator] = []
        seen: set[str] = set()
        for s in cands:
            sig = s.model_dump_json()
            if sig in seen:
                continue
            seen.add(sig)
            if self._validate(s, idx):
                kept.append(s)
        # Keep at most one table_cell variant (the best key) to keep artifacts readable.
        pruned, had_table = [], False
        for s in kept:
            if s.kind == "table_cell":
                if had_table:
                    continue
                had_table = True
            pruned.append(s)
        final = pruned or [cands[-1]]
        return Target(description=_describe(el, final), control=c, strategies=final)

    # ----------------------------------------------------------------- replay

    def resolve(self, target: Target) -> Resolution:
        misses: list[str] = []
        for rank, s in enumerate(target.strategies):
            token = pysecrets.token_hex(4)
            try:
                res = self._eval(js.RESOLVE, {"strategy": s.model_dump(), "token": token, "idx": None})
            except PWError as e:
                misses.append(f"{s.kind}: error {e.__class__.__name__}")
                continue
            n = res["count"] if res else 0
            if n == 1:
                return Resolution(self.page.locator(f'[data-cua-r="{token}"]').first, s.kind, rank, misses)
            misses.append(f"{s.kind}: {'ambiguous (' + str(n) + ' matches)' if n > 1 else 'not found'}")
        return Resolution(None, None, -1, misses)

    def act(self, handle, action: str, value: str | None, actor: str = "automation") -> None:
        self.control_check(actor)
        self._do(handle, action, value)

    def read(self, handle) -> str:
        return handle.inner_text().strip()

    def navigate(self, url: str, actor: str = "automation") -> None:
        self.control_check(actor)
        ok, reason = self.policy.url_allowed(url)
        if not ok:
            raise PermissionError(f"navigation blocked: {reason}")
        self.page.goto(url, wait_until="domcontentloaded")

    def click_text(self, text: str, actor: str = "automation") -> bool:
        self.control_check(actor)
        res = self.resolve(Target(description=text, control="clickable", strategies=[TextLocator(text=text)]))
        if res.handle is None:
            return False
        self._do(res.handle, "click", None)
        return True

    def fill_by_label(self, label: str, value: str, actor: str = "automation") -> bool:
        self.control_check(actor)
        res = self.resolve(Target(description=label, control="input", strategies=[LabelLocator(label=label, control="input")]))
        if res.handle is None:
            return False
        self._do(res.handle, "fill", value)
        return True

    def _do(self, loc, action: str, value: str | None) -> None:
        t = self.action_timeout_ms
        if action == "click":
            loc.click(timeout=t)
        elif action == "fill":
            loc.fill(value or "", timeout=t)
        elif action == "select":
            loc.select_option(label=value, timeout=t)
        elif action == "press":
            loc.press(value or "Enter", timeout=t)
        else:
            raise ValueError(f"unsupported action {action}")
        self.settle()

    # --------------------------------------------------------------- evidence

    def screenshot(self, path: str) -> str:
        """Full-page screenshot with PII masked before the pixels hit disk."""
        masks = [self.page.get_by_text(re.compile(p)) for p in SCREEN_MASK_PATTERNS]
        masks.append(self.page.locator("input[type=password]"))
        try:
            self.page.screenshot(path=path, full_page=True, mask=masks, mask_color="#000000")
        except PWError:
            self.page.screenshot(path=path, full_page=True)
        return path

    def screenshot_bytes(self) -> bytes:
        masks = [self.page.get_by_text(re.compile(p)) for p in SCREEN_MASK_PATTERNS]
        return self.page.screenshot(full_page=True, mask=masks, mask_color="#000000")

    def dom_snapshot(self, path: str) -> str:
        try:
            html = self.page.content()
        except PWError:
            html = "<unavailable>"
        html = re.sub(r"data-cua-(idx|r)=\"[^\"]*\"", "", html)
        with open(path, "w") as f:
            f.write(self.redactor.text(html))
        return path


def _describe(el: UIElement, strategies: list) -> str:
    """Reviewer-facing description built from *chrome* (labels, headers, button
    text), never from record data in the row."""
    tc = next((s for s in strategies if s.kind == "table_cell"), None)
    if el.control in ("input", "select"):
        kind = "password field" if el.input_type == "password" else ("dropdown" if el.control == "select" else "text field")
        return f'{kind} next to "{el.label}"' if el.label else f"{kind} ({el.tag})"
    if el.control == "clickable":
        if tc:
            return f'link/control in column "{tc.column}" of the row containing "{tc.row_contains}"'
        return f'"{el.text or el.name}" ({el.tag}{", no button role" if not el.role else ""})'
    if tc:
        return f'value in column "{tc.column}" of the row containing "{tc.row_contains}"'
    lab = next((s for s in strategies if s.kind == "label"), None)
    return f'text right of "{lab.label}"' if lab else "text at recorded position"
