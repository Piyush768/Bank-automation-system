"""The seam between *how we perceive/act on a surface* and *the recorded flow*.

Discovery, compilation and replay only ever talk to ``Surface``. They see
``UIElement``s described in operator terms (control kind, visible label, row
text, column header, bounding box) and ``Target``s made of surface-neutral
locator strategies. A new surface (legacy web in frames, Windows UIA desktop
app, 3270 terminal) is a new ``Surface`` implementation; the artifact schema,
the replay engine, and the error taxonomy do not change.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field

from pydantic import BaseModel

from cua.schema.artifact import Target


class UIElement(BaseModel):
    idx: int
    control: str  # input | select | clickable | text
    tag: str = ""
    input_type: str = ""
    role: str = ""
    name: str = ""
    label: str = ""
    text: str = ""
    value: str = ""
    row_text: str = ""
    column_header: str = ""
    effect_text: str = ""  # what committing via this control would "say" (for risk)
    bbox: tuple[float, float, float, float] = (0, 0, 0, 0)
    css: str = ""


class Observation(BaseModel):
    url: str
    title: str
    landmarks: list[str]
    elements: list[UIElement]
    page_text: str
    viewport: tuple[int, int] = (1280, 900)

    def element(self, idx: int) -> UIElement | None:
        return next((e for e in self.elements if e.idx == idx), None)


@dataclass
class Resolution:
    handle: object | None
    strategy: str | None = None
    rank: int = -1
    misses: list[str] = field(default_factory=list)


class Surface(ABC):
    # ---- perception ----
    @abstractmethod
    def observe(self) -> Observation: ...

    @abstractmethod
    def page_text(self) -> str: ...

    @abstractmethod
    def landmarks(self) -> list[str]: ...

    @abstractmethod
    def current_url(self) -> str: ...

    # ---- discovery-time: act by observation index, record robust targets ----
    @abstractmethod
    def act_on_index(self, idx: int, action: str, value: str | None) -> None: ...

    @abstractmethod
    def read_index(self, idx: int) -> str: ...

    @abstractmethod
    def describe_target(self, idx: int, obs: Observation, params: dict[str, str]) -> Target: ...

    # ---- replay-time: resolve recorded targets, act on handles ----
    @abstractmethod
    def resolve(self, target: Target) -> Resolution: ...

    @abstractmethod
    def act(self, handle: object, action: str, value: str | None) -> None: ...

    @abstractmethod
    def read(self, handle: object) -> str: ...

    @abstractmethod
    def navigate(self, url: str) -> None: ...

    @abstractmethod
    def click_text(self, text: str) -> bool: ...

    # ---- evidence / control ----
    @abstractmethod
    def screenshot(self, path: str) -> str: ...

    @abstractmethod
    def dom_snapshot(self, path: str) -> str: ...

    @abstractmethod
    def settle(self) -> None: ...

    @abstractmethod
    def pump(self, ms: int = 100) -> None:
        """Let the surface process events while automation is paused."""
