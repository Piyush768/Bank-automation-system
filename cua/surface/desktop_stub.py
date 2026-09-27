"""Desktop surface: a documented seam, deliberately not implemented.

How it would map onto the same abstractions (Windows UI Automation via
pywinauto/uiautomation, or Java Access Bridge for Swing apps):

* observe()      walk the UIA tree of the target window; each control becomes
                 a UIElement: ControlType -> control/role, Name -> name,
                 LabeledBy / nearest static text to the left -> label,
                 DataGrid row/column -> row_text / column_header,
                 BoundingRectangle -> bbox.
* resolve()      LabelLocator  -> find static text == label, take the edit
                                  control to its right (same algorithm as web)
                 TableCellLocator -> DataGrid row whose cells contain text, by
                                  column header
                 RoleLocator   -> ControlType + Name
                 TextLocator   -> Button/Hyperlink with Name == text
                 CoordsLocator -> click at point (degraded)
                 CssLocator    -> not applicable on this surface (skipped)
* act()          Invoke/Value/SelectionItem patterns, falling back to
                 synthesized mouse/keyboard input.
* screenshot()   window capture with PII regions masked from OCR boxes.

For pixel-only surfaces (Citrix, RDP) observe() would come from OCR +
layout detection instead of an accessibility tree; LabelLocator and
TableCellLocator still apply because they are defined in visual terms.
Nothing in the artifact schema or replay engine changes.
"""

from cua.surface.base import Surface


class DesktopSurface(Surface):  # pragma: no cover - seam only
    def __init__(self, *_, **__):
        raise NotImplementedError("Desktop surface is a documented seam; see module docstring.")
