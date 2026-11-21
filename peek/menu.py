"""The status-item dropdown (Figma frame 3:3).

Order: header ("Peek" + status line), separator, Capture & solve ⌃⌃,
Show answers ⌘⌘, disabled "Last capture: …", separator, Hard mode (switch; owner's
addition, not in Figma), separator, Settings… ⌘,, Quit Peek ⌘Q.
"""

from __future__ import annotations

import objc
from AppKit import (
    NSAttributedString,
    NSBezierPath,
    NSColor,
    NSControlSizeSmall,
    NSControlStateValueOff,
    NSControlStateValueOn,
    NSEventModifierFlagCommand,
    NSEventModifierFlagControl,
    NSEventModifierFlagShift,
    NSFont,
    NSFontAttributeName,
    NSFontWeightSemibold,
    NSForegroundColorAttributeName,
    NSLineBreakByTruncatingTail,
    NSMakeRect,
    NSMenu,
    NSMenuItem,
    NSSwitch,
    NSTextField,
    NSView,
    NSViewMinXMargin,
    NSViewWidthSizable,
)

from peek.providers.catalog import PROVIDER_ORDER, PROVIDERS
from peek.status import HeaderStatus
from peek.ui_common import StatusDotView

MENU_WIDTH = 262.0
HEADER_HEIGHT = 49.0
# Left edge of the header text. Matches where stock menu item titles start on
# macOS 26 (tuned against a screenshot).
HEADER_INSET = 14.0
HARD_MODE_HEIGHT = 40.0


class MenuHeaderView(NSView):
    """'Peek' in 13 pt semibold over a status row: 8 pt dot + 12 pt secondary text."""

    def initWithFrame_(self, frame):
        self = objc.super(MenuHeaderView, self).initWithFrame_(frame)
        if self is None:
            return None
        width = frame.size.width

        title = NSTextField.labelWithString_("Peek")
        title.setFont_(NSFont.systemFontOfSize_weight_(13, NSFontWeightSemibold))
        title.setTextColor_(NSColor.labelColor())
        title.setFrame_(NSMakeRect(HEADER_INSET, 6, width - HEADER_INSET - 10, 16))
        title.setAutoresizingMask_(NSViewWidthSizable)

        dot = StatusDotView.alloc().initWithFrame_(NSMakeRect(HEADER_INSET, 26 + 3.5, 8, 8))

        status = NSTextField.labelWithString_("")
        status.setFont_(NSFont.systemFontOfSize_(12))
        status.setTextColor_(NSColor.secondaryLabelColor())
        status.setLineBreakMode_(NSLineBreakByTruncatingTail)
        status.setFrame_(NSMakeRect(HEADER_INSET + 14, 26, width - HEADER_INSET - 14 - 10, 15))
        status.setAutoresizingMask_(NSViewWidthSizable)

        for view in (title, dot, status):
            self.addSubview_(view)
        self._dot = dot
        self._status = status
        self.setAutoresizingMask_(NSViewWidthSizable)
        return self

    def isFlipped(self):
        return True

    @objc.python_method
    def set_status(self, status: HeaderStatus) -> None:
        self._status.setStringValue_(status.text)
        self._dot.set_dot(status.dot)


class HardModeView(NSView):
    """'Hard mode' over a secondary '<best model> · costs more', with a switch
    on the right. A view, not a checkmark item: a state column would shift every
    title in the menu away from the header's inset."""

    def initWithFrame_(self, frame):
        self = objc.super(HardModeView, self).initWithFrame_(frame)
        if self is None:
            return None
        width = frame.size.width

        switch = NSSwitch.alloc().initWithFrame_(NSMakeRect(0, 0, 0, 0))
        switch.setControlSize_(NSControlSizeSmall)
        switch.sizeToFit()
        sw, sh = switch.frame().size.width, switch.frame().size.height
        top = (frame.size.height - sh) / 2
        switch.setFrame_(NSMakeRect(width - HEADER_INSET - sw, top, sw, sh))
        switch.setAutoresizingMask_(NSViewMinXMargin)
        switch.setAccessibilityLabel_("Hard mode")

        text_width = width - HEADER_INSET * 2 - sw - 8
        title = NSTextField.labelWithString_("Hard mode")
        title.setFont_(NSFont.menuFontOfSize_(0))
        title.setTextColor_(NSColor.labelColor())
        title.setFrame_(NSMakeRect(HEADER_INSET, 4, text_width, 17))

        note = NSTextField.labelWithString_("")
        note.setFont_(NSFont.systemFontOfSize_(11))
        note.setTextColor_(NSColor.secondaryLabelColor())
        note.setLineBreakMode_(NSLineBreakByTruncatingTail)
        note.setFrame_(NSMakeRect(HEADER_INSET, 21, text_width, 14))

        for view in (title, note, switch):
            self.addSubview_(view)
        self.switch = switch
        self._title = title
        self._note = note
        self._highlighted = False
        self.setAutoresizingMask_(NSViewWidthSizable)
        return self

    def isFlipped(self):
        return True

    def drawRect_(self, rect):
        # A view-based item draws its own highlight (keyboard navigation, hover).
        item = self.enclosingMenuItem()
        highlighted = item is not None and item.isHighlighted()
        if highlighted:
            NSColor.selectedContentBackgroundColor().setFill()
            NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
                NSMakeRect(5, 0, self.bounds().size.width - 10, self.bounds().size.height), 5, 5
            ).fill()
        if highlighted != self._highlighted:  # recolour only on a change (no redraw loop)
            self._highlighted = highlighted
            selected = NSColor.selectedMenuItemTextColor()
            self._title.setTextColor_(selected if highlighted else NSColor.labelColor())
            self._note.setTextColor_(selected if highlighted else NSColor.secondaryLabelColor())

    def mouseUp_(self, event):
        # A click anywhere on the row flips the switch (clicks on the switch
        # itself are handled by the switch).
        self.switch.performClick_(None)

    @objc.python_method
    def set_state(self, on: bool, best_model_name: str) -> None:
        self.switch.setState_(NSControlStateValueOn if on else NSControlStateValueOff)
        self._note.setStringValue_(f"{best_model_name} · costs more")
        self.setToolTip_(
            f"Answer with {best_model_name}, the most capable model, until you turn this "
            "off. It is slower and each capture costs several times more. Off, Peek goes "
            "back to the model chosen in Settings."
        )


def _item(title: str, action: str | None, key: str = "", mask: int = 0, target=None):
    item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(title, action, key)
    if mask:
        item.setKeyEquivalentModifierMask_(mask)
    if target is not None:
        item.setTarget_(target)
    return item


def _section_header(title: str) -> NSMenuItem:
    """A small grey heading over a group of items (macOS 14's section header)."""
    if hasattr(NSMenuItem, "sectionHeaderWithTitle_"):
        return NSMenuItem.sectionHeaderWithTitle_(title)
    item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(title, None, "")
    item.setEnabled_(False)
    return item


def _info_title(text: str) -> NSAttributedString:
    return NSAttributedString.alloc().initWithString_attributes_(
        text,
        {
            NSFontAttributeName: NSFont.systemFontOfSize_(12),
            NSForegroundColorAttributeName: NSColor.secondaryLabelColor(),
        },
    )


class StatusMenu:
    """Builds the NSMenu and exposes small update methods (main thread only)."""

    def __init__(self, target, *, delegate=None) -> None:
        menu = NSMenu.alloc().initWithTitle_("Peek")
        menu.setAutoenablesItems_(False)
        menu.setMinimumWidth_(MENU_WIDTH)
        if delegate is not None:
            menu.setDelegate_(delegate)

        self.header = MenuHeaderView.alloc().initWithFrame_(
            NSMakeRect(0, 0, MENU_WIDTH, HEADER_HEIGHT)
        )
        header_item = NSMenuItem.alloc().init()
        header_item.setView_(self.header)
        menu.addItem_(header_item)
        menu.addItem_(NSMenuItem.separatorItem())

        # "⌃⌃", "⌘⌘" and "⇧⇧" are double-taps, not real key equivalents. Using the modifier
        # glyph itself as the key character makes AppKit draw "⌃⌃" / "⌘⌘" in the
        # native shortcut column; no keyboard can type those characters, so the
        # items never fire from the keyboard.
        self.capture = _item(
            "Capture & solve", "captureAndSolve:", "⌃", NSEventModifierFlagControl, target
        )
        self.toggle = _item(
            "Show answers", "toggleAnswers:", "⌘", NSEventModifierFlagCommand, target
        )
        self.toggle.setEnabled_(False)
        # Only while Settings → Show work is on (or its working is still on screen).
        self.work_toggle = _item("Show work", "toggleWork:", "⇧", NSEventModifierFlagShift, target)
        self.work_toggle.setEnabled_(False)
        self.work_toggle.setHidden_(True)
        self.last_capture = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_("", None, "")
        self.last_capture.setEnabled_(False)
        self.last_capture.setHidden_(True)
        # Shown only after a ChatGPT usage-limit error (spec: "Manage usage" link).
        self.manage_usage = _item("Manage ChatGPT usage…", "manageUsage:", target=target)
        self.manage_usage.setHidden_(True)
        for item in (
            self.capture,
            self.toggle,
            self.work_toggle,
            self.last_capture,
            self.manage_usage,
        ):
            menu.addItem_(item)
        menu.addItem_(NSMenuItem.separatorItem())

        # Quick switch: each provider with the model chosen for it in Settings.
        menu.addItem_(_section_header("AI provider"))
        self.provider_items: dict[str, NSMenuItem] = {}
        for pid in PROVIDER_ORDER:
            item = _item(PROVIDERS[pid].name, "chooseProvider:", target=target)
            item.setRepresentedObject_(pid)
            self.provider_items[pid] = item
            menu.addItem_(item)
        menu.addItem_(NSMenuItem.separatorItem())

        self.hard_mode = HardModeView.alloc().initWithFrame_(
            NSMakeRect(0, 0, MENU_WIDTH, HARD_MODE_HEIGHT)
        )
        self.hard_mode.switch.setTarget_(target)
        self.hard_mode.switch.setAction_("toggleHardMode:")
        hard_item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
            "Hard mode", "toggleHardMode:", ""
        )
        hard_item.setTarget_(target)  # Return on the highlighted row (keyboard)
        hard_item.setView_(self.hard_mode)
        self.hard_mode_item = hard_item
        menu.addItem_(hard_item)
        menu.addItem_(NSMenuItem.separatorItem())

        self.settings = _item("Settings…", "openSettings:", ",", NSEventModifierFlagCommand, target)
        self.quit = _item("Quit Peek", "terminate:", "q", NSEventModifierFlagCommand)
        menu.addItem_(self.settings)
        menu.addItem_(self.quit)

        self.diagnostics_item: NSMenuItem | None = None
        self.menu = menu

    def set_status(self, status: HeaderStatus) -> None:
        self.header.set_status(status)

    def set_manage_usage(self, visible: bool) -> None:
        self.manage_usage.setHidden_(not visible)

    def set_hard_mode(self, on: bool, best_model_name: str) -> None:
        self.hard_mode.set_state(on, best_model_name)

    def set_answers(self, *, available: bool, visible: bool) -> None:
        self.toggle.setEnabled_(available)
        self.toggle.setTitle_("Hide answers" if (available and visible) else "Show answers")

    def set_work(self, *, shown: bool, available: bool, visible: bool) -> None:
        self.work_toggle.setHidden_(not shown)
        self.work_toggle.setEnabled_(available)
        self.work_toggle.setTitle_("Hide work" if (available and visible) else "Show work")

    def set_providers(self, current: str, models: dict[str, str]) -> None:
        """A check by the provider captures use; under each name, its model."""
        for pid, item in self.provider_items.items():
            item.setState_(NSControlStateValueOn if pid == current else NSControlStateValueOff)
            model = models.get(pid, "")
            if item.respondsToSelector_("setSubtitle:"):  # macOS 14.4 and later
                item.setSubtitle_(model)
            else:
                item.setToolTip_(model)

    def set_last_capture(self, text: str | None) -> None:
        if text is None:
            self.last_capture.setHidden_(True)
            return
        self.last_capture.setAttributedTitle_(_info_title(text))
        self.last_capture.setHidden_(False)

    def add_diagnostics(self, submenu: NSMenu) -> None:
        """Debug builds only: a Diagnostics submenu above Quit."""
        item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_("Diagnostics", None, "")
        item.setSubmenu_(submenu)
        index = self.menu.indexOfItem_(self.settings)
        self.menu.insertItem_atIndex_(item, index)
        self.diagnostics_item = item
