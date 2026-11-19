"""First-run window (Figma 3:63): two permissions, then connect a provider.

Step 1, "Peek needs two permissions": a card each for Screen Recording and Input
Monitoring, with "Granted" or "Open System Settings". States are polled once a
second. macOS often applies a grant only to a new process, so
after "Open System Settings" the card also offers "Quit & Reopen"; onboarding
comes back after the relaunch until it is finished. "Continue" is enabled when
both are granted.

Step 2, "Connect an AI provider": the Settings Provider pane itself (Claude
preselected; owner-approved default: no separate design). "Done" finishes
onboarding; closing the window also leaves it for later.
"""

from __future__ import annotations

import logging
from collections.abc import Callable

import objc
from AppKit import (
    NSBackingStoreBuffered,
    NSBezelStyleRounded,
    NSBezierPath,
    NSButton,
    NSColor,
    NSFont,
    NSFontWeightSemibold,
    NSImage,
    NSImageView,
    NSInsetRect,
    NSLayoutAttributeCenterY,
    NSLayoutAttributeLeading,
    NSLineBreakByWordWrapping,
    NSMakeRect,
    NSObject,
    NSStackView,
    NSTextField,
    NSTimer,
    NSUserInterfaceLayoutOrientationHorizontal,
    NSUserInterfaceLayoutOrientationVertical,
    NSView,
    NSWindow,
    NSWindowStyleMaskClosable,
    NSWindowStyleMaskTitled,
)
from Foundation import NSEdgeInsetsMake

from peek import permissions
from peek.log import entry_point
from peek.status import DotColor
from peek.ui_common import CardView, StatusDotView

log = logging.getLogger(__name__)

WIDTH = 560
INNER = 496
POLL_S = 1.0
SCREEN, INPUT = "screen", "input"
CARDS = {
    SCREEN: (
        "display",
        "Screen Recording",
        "Lets Peek take the screenshot you ask for.",
        permissions.SCREEN_RECORDING_URL,
    ),
    INPUT: (
        "keyboard",
        "Input Monitoring",
        "Lets Peek notice a double-tap on Control or Command.",
        permissions.INPUT_MONITORING_URL,
    ),
}


def _label(text: str, size: float = 13, *, secondary=False, weight=None, wrap=0) -> NSTextField:
    field = NSTextField.labelWithString_(text)
    field.setFont_(
        NSFont.systemFontOfSize_weight_(size, weight) if weight else NSFont.systemFontOfSize_(size)
    )
    field.setTextColor_(NSColor.secondaryLabelColor() if secondary else NSColor.labelColor())
    if wrap:
        field.setLineBreakMode_(NSLineBreakByWordWrapping)
        field.setPreferredMaxLayoutWidth_(wrap)
        field.cell().setWraps_(True)
    return field


def _stack(views, *, vertical: bool, spacing: float, align=None) -> NSStackView:
    s = NSStackView.stackViewWithViews_(views)
    s.setOrientation_(
        NSUserInterfaceLayoutOrientationVertical
        if vertical
        else NSUserInterfaceLayoutOrientationHorizontal
    )
    s.setSpacing_(spacing)
    if align is not None:
        s.setAlignment_(align)
    return s


def waiting_text(screen: bool, input_: bool) -> str:
    """The footer status (Figma: "Waiting for Input Monitoring…")."""
    if not screen and not input_:
        return "Waiting for both permissions…"
    if not screen:
        return "Waiting for Screen Recording…"
    if not input_:
        return "Waiting for Input Monitoring…"
    return ""


class IconTile(NSView):
    """32x32 rounded tile behind a permission's glyph."""

    def drawRect_(self, _rect):
        NSColor.quaternaryLabelColor().setFill()
        NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
            NSInsetRect(self.bounds(), 0, 0), 8, 8
        ).fill()

    def isOpaque(self):
        return False


class OnboardingController(NSObject):
    """Owns the onboarding window. Main thread only."""

    @objc.python_method
    def configure(
        self,
        *,
        build_provider_pane: Callable[[], NSView],
        on_done: Callable[[], None],
        relaunch: Callable[[], None],
        screen_granted: Callable[[], bool] = permissions.screen_recording_granted,
        input_granted: Callable[[], bool] = permissions.input_monitoring_granted,
        request: dict[str, Callable[[], object]] | None = None,
        open_settings: Callable[[str], None] = permissions.open_system_settings,
    ) -> None:
        self._build_provider_pane = build_provider_pane
        self._on_done = on_done
        self._relaunch = relaunch
        self._granted = {SCREEN: screen_granted, INPUT: input_granted}
        self._request = request or {
            SCREEN: permissions.request_screen_recording,
            INPUT: permissions.request_input_monitoring,
        }
        self._open_settings = open_settings
        self._asked: set[str] = set()
        self.window = None
        self._timer = None
        self.step = 1

    # --- window ---------------------------------------------------------------

    @objc.python_method
    def show(self, bring_to_front: Callable) -> None:
        if self.window is None:
            self.window = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
                NSMakeRect(0, 0, WIDTH, 362),
                NSWindowStyleMaskTitled | NSWindowStyleMaskClosable,
                NSBackingStoreBuffered,
                False,
            )
            self.window.setTitle_("Welcome to Peek")
            self.window.setReleasedWhenClosed_(False)
            self.window.setTitlebarSeparatorStyle_(1)  # NSTitlebarSeparatorStyleNone
            self.window.setDelegate_(self)  # windowWillClose_ below
            self._show_step1()
            self.window.center()
        bring_to_front(self.window)
        self._start_polling()

    @objc.python_method
    def _set_content(self, content: NSView) -> None:
        content.layoutSubtreeIfNeeded()
        self.window.setContentView_(content)
        size = content.fittingSize()
        self.window.setContentSize_((WIDTH, size.height))

    @objc.python_method
    def _start_polling(self) -> None:
        if self._timer is None and self.step == 1:
            self._timer = NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
                POLL_S, self, "poll:", None, True
            )

    @objc.python_method
    def _stop_polling(self) -> None:
        if self._timer is not None:
            self._timer.invalidate()
            self._timer = None

    # --- step 1 -------------------------------------------------------------------

    @objc.python_method
    def _show_step1(self) -> None:
        self.step = 1
        self.cards: dict[str, dict] = {}
        rows = [self._card(kind) for kind in (SCREEN, INPUT)]
        self.waiting = _label("", 12, secondary=True)
        self.continue_button = NSButton.buttonWithTitle_target_action_(
            "Continue", self, "continue:"
        )
        self.continue_button.setBezelStyle_(NSBezelStyleRounded)
        self.continue_button.setKeyEquivalent_("\r")
        spacer = NSView.alloc().initWithFrame_(NSMakeRect(0, 0, 1, 1))
        footer = _stack(
            [spacer, self.waiting, self.continue_button],
            vertical=False,
            spacing=10,
            align=NSLayoutAttributeCenterY,
        )
        footer.setTranslatesAutoresizingMaskIntoConstraints_(False)
        footer.widthAnchor().constraintEqualToConstant_(INNER).setActive_(True)
        content = _stack(
            [
                _label("STEP 1 OF 2", 11, secondary=True, weight=NSFontWeightSemibold),
                _label("Peek needs two permissions", 20, weight=NSFontWeightSemibold),
                _label(
                    "macOS asks you to approve these once. Peek only looks at your screen "
                    "when you double-tap Control.",
                    secondary=True,
                    wrap=INNER,
                ),
                *rows,
                footer,
            ],
            vertical=True,
            spacing=14,
            align=NSLayoutAttributeLeading,
        )
        content.setEdgeInsets_(NSEdgeInsetsMake(20, 32, 28, 32))
        self._set_content(content)
        self.refresh()

    @objc.python_method
    def _card(self, kind: str) -> NSView:
        symbol, title, description, _url = CARDS[kind]
        tile = IconTile.alloc().initWithFrame_(NSMakeRect(0, 0, 32, 32))
        tile.setTranslatesAutoresizingMaskIntoConstraints_(False)
        tile.widthAnchor().constraintEqualToConstant_(32).setActive_(True)
        tile.heightAnchor().constraintEqualToConstant_(32).setActive_(True)
        icon = NSImageView.imageViewWithImage_(
            NSImage.imageWithSystemSymbolName_accessibilityDescription_(symbol, title)
        )
        icon.setContentTintColor_(NSColor.labelColor())
        icon.setFrame_(NSMakeRect(6, 6, 20, 20))
        tile.addSubview_(icon)
        text = _stack(
            [
                _label(title, weight=NSFontWeightSemibold),
                _label(description, 12, secondary=True, wrap=243),
            ],
            vertical=True,
            spacing=2,
            align=NSLayoutAttributeLeading,
        )
        dot = StatusDotView.alloc().initWithFrame_(NSMakeRect(0, 0, 8, 8))
        dot.setTranslatesAutoresizingMaskIntoConstraints_(False)
        dot.widthAnchor().constraintEqualToConstant_(8).setActive_(True)
        dot.heightAnchor().constraintEqualToConstant_(8).setActive_(True)
        dot.set_dot(DotColor.SUCCESS)
        granted = _stack([dot, _label("Granted", 12, secondary=True)], vertical=False, spacing=6)
        open_button = NSButton.buttonWithTitle_target_action_(
            "Open System Settings", self, "openSettings:"
        )
        open_button.setBezelStyle_(NSBezelStyleRounded)
        open_button.setBezelColor_(NSColor.controlAccentColor())
        open_button.setTag_(0 if kind == SCREEN else 1)
        reopen = NSButton.buttonWithTitle_target_action_("Quit & Reopen", self, "reopen:")
        reopen.setBezelStyle_(NSBezelStyleRounded)
        reopen.setToolTip_("macOS applies the permission after Peek restarts.")
        actions = _stack([reopen, open_button], vertical=False, spacing=8)
        spacer = NSView.alloc().initWithFrame_(NSMakeRect(0, 0, 1, 1))
        spacer.setContentHuggingPriority_forOrientation_(1, 0)
        row = _stack(
            [tile, text, spacer, granted, actions],
            vertical=False,
            spacing=12,
            align=NSLayoutAttributeCenterY,
        )
        card = CardView.alloc().initWithFrame_(NSMakeRect(0, 0, INNER, 60))
        card.set_content(row, padding=14)
        card.widthAnchor().constraintEqualToConstant_(INNER).setActive_(True)
        row.trailingAnchor().constraintEqualToAnchor_constant_(
            card.trailingAnchor(), -14
        ).setActive_(True)
        self.cards[kind] = {"granted": granted, "open": open_button, "reopen": reopen}
        return card

    @objc.python_method
    def refresh(self) -> tuple[bool, bool]:
        state = {kind: bool(check()) for kind, check in self._granted.items()}
        for kind, card in self.cards.items():
            ok = state[kind]
            card["granted"].setHidden_(not ok)
            card["open"].setHidden_(ok)
            card["reopen"].setHidden_(ok or kind not in self._asked)
        self.continue_button.setEnabled_(state[SCREEN] and state[INPUT])
        self.waiting.setStringValue_(waiting_text(state[SCREEN], state[INPUT]))
        return state[SCREEN], state[INPUT]

    @entry_point
    def poll_(self, _timer):
        self.refresh()

    @entry_point
    def openSettings_(self, sender):
        kind = SCREEN if sender.tag() == 0 else INPUT
        self._asked.add(kind)
        try:
            self._request[kind]()  # the system prompt; also lists Peek in Settings
        except Exception:
            log.exception("Permission request failed")
        self._open_settings(CARDS[kind][3])
        self.refresh()

    @entry_point
    def reopen_(self, _sender):
        self._relaunch()

    @entry_point
    def continue_(self, _sender):
        if not all(self.refresh()):
            return
        self._stop_polling()
        self._show_step2()

    # --- step 2 -------------------------------------------------------------------

    @objc.python_method
    def _show_step2(self) -> None:
        self.step = 2
        self.done_button = NSButton.buttonWithTitle_target_action_("Done", self, "done:")
        self.done_button.setBezelStyle_(NSBezelStyleRounded)
        self.done_button.setKeyEquivalent_("\r")
        pane = self._build_provider_pane()
        spacer = NSView.alloc().initWithFrame_(NSMakeRect(0, 0, 1, 1))
        footer = _stack([spacer, self.done_button], vertical=False, spacing=10)
        footer.setTranslatesAutoresizingMaskIntoConstraints_(False)
        footer.widthAnchor().constraintEqualToConstant_(INNER + 40).setActive_(True)
        header = _stack(
            [
                _label("STEP 2 OF 2", 11, secondary=True, weight=NSFontWeightSemibold),
                _label("Connect an AI provider", 20, weight=NSFontWeightSemibold),
            ],
            vertical=True,
            spacing=14,
            align=NSLayoutAttributeLeading,
        )
        header.setEdgeInsets_(NSEdgeInsetsMake(20, 32, 0, 32))
        content = _stack(
            [header, pane, footer], vertical=True, spacing=0, align=NSLayoutAttributeLeading
        )
        content.setEdgeInsets_(NSEdgeInsetsMake(0, 0, 24, 0))
        self.window.setContentView_(content)
        content.layoutSubtreeIfNeeded()
        self.window.setContentSize_((WIDTH + 44, content.fittingSize().height))

    @entry_point
    def done_(self, _sender):
        self._stop_polling()
        if self.window is not None:
            # A key pasted but not yet committed (no Return): ending editing fires
            # the field's action, which saves it, before the window goes.
            self.window.makeFirstResponder_(None)
            self.window.orderOut_(None)
        self._on_done()

    @entry_point
    def windowWillClose_(self, _notification):
        # Closed with the red button or ⌘W: commit any key being typed, stop the
        # permission poll. Onboarding comes back at the next launch.
        self.window.makeFirstResponder_(None)
        self._stop_polling()
