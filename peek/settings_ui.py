"""Settings window: sidebar + three panes (Figma frames 3:101, 3:270, 3:340).

- Provider: heading, provider segmented control, the provider's panel (Claude:
  API key, Test connection + status, Model pop-up), footer.
- Hotkeys & capture: the two double-tap keycaps, the double-tap speed slider
  (200-500 ms), which display to capture, launch at login (SMAppService).
- Overlay: answer text size ("Match the question" or a fixed size), keep answers
  visible across captures, and a preview of the owner's answer style (plain text
  in the question's colour, the multiple-choice letter in yellow). No background
  opacity: answers have no background (owner's change to the design).

Non-secret settings are saved to settings.json on every change (store.py), and
`on_change` tells the app. Keychain work runs on background threads (after a
rebuild the Keychain may ask for the login password, which must not freeze the
window).
"""

from __future__ import annotations

import logging
import os
from collections.abc import Callable

import objc
from AppKit import (
    NSBackingStoreBuffered,
    NSBezierPath,
    NSButton,
    NSColor,
    NSColorSpace,
    NSColorWell,
    NSControlStateValueOff,
    NSControlStateValueOn,
    NSFont,
    NSFontWeightSemibold,
    NSLayoutAttributeCenterY,
    NSLayoutAttributeLeading,
    NSLayoutAttributeTop,
    NSLineBreakByTruncatingMiddle,
    NSLineBreakByTruncatingTail,
    NSLineBreakByWordWrapping,
    NSMakeRect,
    NSModalResponseOK,
    NSObject,
    NSOpenPanel,
    NSPopUpButton,
    NSScrollView,
    NSSegmentedControl,
    NSSegmentStyleRounded,
    NSSlider,
    NSStackView,
    NSSwitch,
    NSTableCellView,
    NSTableColumn,
    NSTableView,
    NSTableViewStyleSourceList,
    NSTextAlignmentRight,
    NSTextField,
    NSUserInterfaceLayoutOrientationHorizontal,
    NSUserInterfaceLayoutOrientationVertical,
    NSView,
    NSWindow,
    NSWindowStyleMaskClosable,
    NSWindowStyleMaskTitled,
)
from Foundation import NSURL, NSEdgeInsetsMake

from peek import store
from peek.log import entry_point
from peek.provider_panels import ChatGPTPanel, KeyProviderPanel
from peek.providers.catalog import PROVIDERS
from peek.ui_common import AnswerPreviewView, CardView, KeycapView
from peek.work_box_view import WorkBoxView

log = logging.getLogger(__name__)

WINDOW_WIDTH = 720
BODY_HEIGHT = 483  # Figma: 520 minus the 37 pt title bar
SIDEBAR_WIDTH = 189
PANES = ("Provider", "Hotkeys & capture", "Overlay", "Show work")
SIDEBAR_CELL = "pane-cell"
SEGMENTS = ("claude", "chatgpt", "gemini")  # the provider segmented control, in order
PROVIDER, HOTKEYS, OVERLAY, SHOW_WORK = range(4)

LABEL_WIDTH = 92  # Claude panel (frame 3:101)
PANE_LABEL_WIDTH = 120  # Hotkeys & capture and Overlay rows (frames 3:270, 3:340)
CONTROL_WIDTH = 260
CONTENT_WIDTH = 475
SPEED_NOTE = "Both taps must land within this window. Regular shortcuts like ⌘C never trigger it."
DISPLAY_CHOICES = (("under_pointer", "Display under the pointer"), ("all", "All displays"))
MATCH_LABEL = "Match the question"
NO_FOLDER = "No folder chosen"
WORK_NOTE = "Each question's working, in the box below. Uses more of your credit or plan."
WORK_BOX_HINT = "Drag the box to move it, or an edge or corner to resize it. Arrow keys work too."
WORK_BOX_HEIGHT = 226
PREVIEW_MATCH_SIZE = 15.0  # the preview's question text, Figma


def _label(text: str, size: float = 13, *, secondary: bool = False, weight=None) -> NSTextField:
    field = NSTextField.labelWithString_(text)
    field.setFont_(
        NSFont.systemFontOfSize_weight_(size, weight) if weight else NSFont.systemFontOfSize_(size)
    )
    field.setTextColor_(NSColor.secondaryLabelColor() if secondary else NSColor.labelColor())
    return field


def _wrapping(text: str, width: float) -> NSTextField:
    field = _label(text, 12, secondary=True)
    field.setLineBreakMode_(NSLineBreakByWordWrapping)
    field.setPreferredMaxLayoutWidth_(width)
    field.cell().setWraps_(True)
    return field


def _fixed_width(view: NSView, width: float) -> NSView:
    view.setTranslatesAutoresizingMaskIntoConstraints_(False)
    view.widthAnchor().constraintEqualToConstant_(width).setActive_(True)
    return view


def _fixed_size(view: NSView, width: float, height: float) -> NSView:
    _fixed_width(view, width)
    view.heightAnchor().constraintEqualToConstant_(height).setActive_(True)
    return view


def _stack(views, *, vertical: bool, spacing: float, align=None) -> NSStackView:
    stack = NSStackView.stackViewWithViews_(views)
    stack.setOrientation_(
        NSUserInterfaceLayoutOrientationVertical
        if vertical
        else NSUserInterfaceLayoutOrientationHorizontal
    )
    stack.setSpacing_(spacing)
    if align is not None:
        stack.setAlignment_(align)
    return stack


def _row(
    label_text: str, controls: NSView, *, width: float = LABEL_WIDTH, centered: bool = False
) -> NSStackView:
    """Right-aligned label + controls. Top-aligned rows put the label 4 pt down so
    it centres on a 28 pt control; centred rows suit one-line controls."""
    label = _label(label_text)
    label.setAlignment_(NSTextAlignmentRight)
    _fixed_width(label, width)
    if centered:
        return _stack([label, controls], vertical=False, spacing=14, align=NSLayoutAttributeCenterY)
    holder = _stack([label], vertical=True, spacing=0)
    holder.setEdgeInsets_(NSEdgeInsetsMake(4, 0, 0, 0))
    return _stack([holder, controls], vertical=False, spacing=14, align=NSLayoutAttributeTop)


def _card(rows: list[NSView]) -> CardView:
    stack = _stack(rows, vertical=True, spacing=14, align=NSLayoutAttributeLeading)
    card = CardView.alloc().initWithFrame_(NSMakeRect(0, 0, CONTENT_WIDTH, 100))
    card.set_content(stack, padding=17)
    _fixed_width(card, CONTENT_WIDTH)
    return card


def _heading(text: str) -> NSTextField:
    return _label(text, 15, weight=NSFontWeightSemibold)


def _pane(views: list[NSView], *, spacing_after: dict | None = None) -> NSStackView:
    content = _stack(views, vertical=True, spacing=14, align=NSLayoutAttributeLeading)
    content.setEdgeInsets_(NSEdgeInsetsMake(24, 28, 24, 28))
    for view, gap in (spacing_after or {}).items():
        content.setCustomSpacing_afterView_(gap, view)
    return content


def _switch(target, action: str) -> NSSwitch:
    switch = NSSwitch.alloc().initWithFrame_(NSMakeRect(0, 0, 36, 20))
    switch.setTarget_(target)
    switch.setAction_(action)
    return switch


def _keycaps(glyph: str, caption: str) -> NSStackView:
    width = 30 if glyph == "⌘" else 28  # Figma keycap widths
    caps = []
    for _ in range(2):
        cap = KeycapView.alloc().initWithFrame_(NSMakeRect(0, 0, width, 23))
        cap.set_glyph(glyph)
        caps.append(_fixed_size(cap, width, 23))
    return _stack([*caps, _label(caption, 12, secondary=True)], vertical=False, spacing=6)


def login_item_note(status: int | None) -> str:
    """Text under the Launch at login switch for an SMAppService status."""
    return {
        None: "Not available on this Mac.",
        0: "",  # not registered
        1: "",  # enabled
        2: "Allow Peek in System Settings → General → Login Items.",
        3: "Works once Peek is in your Applications folder.",
    }.get(status, "")


def text_size_title(size: int) -> str:
    return MATCH_LABEL if size == 0 else f"{size} pt"


def folder_title(path: str) -> str:
    """The Save questions folder as shown: '~/Documents/Questions', or a note."""
    if not path:
        return NO_FOLDER
    home = os.path.expanduser("~")
    shown = "~" + path[len(home) :] if path == home or path.startswith(home + "/") else path
    return shown if os.path.isdir(path) else f"{shown} (missing)"


class WindowBackgroundView(NSView):
    """The window's content view: paints the window background itself, so the
    panes render the same in screenshots taken offscreen."""

    def drawRect_(self, rect):
        NSColor.windowBackgroundColor().setFill()
        NSBezierPath.fillRect_(rect)


class SettingsController(NSObject):
    """Owns the Settings window. Main thread only, except the marked workers."""

    @objc.python_method
    def configure(
        self,
        *,
        key_caches: dict[str, store.SecretCache],
        on_change: Callable[[], None],
        on_key_known: Callable[[bool | None, str], None],
        login_service=None,
        choose_folder: Callable[[Callable[[str | None], None]], None] | None = None,
    ) -> None:
        self.key_caches = key_caches
        self._choose_folder = choose_folder  # injected in tests; else an NSOpenPanel
        self.on_change = on_change
        self.on_key_known = on_key_known
        self._login_service = login_service  # injected in tests; else SMAppService
        self.settings = store.load_settings()
        self.window = None
        self._panes: dict[int, NSView] = {}
        self._current = None
        self.panels: dict[str, KeyProviderPanel] = {}
        self._shown_panel = None

    # --- window -------------------------------------------------------------

    @objc.python_method
    def show(self, bring_to_front: Callable, pane: int | None = None) -> None:
        if self.window is None:
            self.window = self._build_window()
        self.settings = store.load_settings()
        self._load_values()
        if pane is not None:
            self.sidebar.selectRowIndexes_byExtendingSelection_(_index_set(pane), False)
        bring_to_front(self.window)

    @objc.python_method
    def _build_window(self):
        window = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            NSMakeRect(0, 0, WINDOW_WIDTH, BODY_HEIGHT),
            NSWindowStyleMaskTitled | NSWindowStyleMaskClosable,
            NSBackingStoreBuffered,
            False,
        )
        window.setTitle_("Peek Settings")
        window.setReleasedWhenClosed_(False)
        window.setDelegate_(self)  # windowWillClose_: commit a key still being edited
        root = WindowBackgroundView.alloc().initWithFrame_(
            NSMakeRect(0, 0, WINDOW_WIDTH, BODY_HEIGHT)
        )

        # Sidebar: a source list (Figma SidebarItem rows, 28 pt).
        table = NSTableView.alloc().initWithFrame_(NSMakeRect(0, 0, SIDEBAR_WIDTH, BODY_HEIGHT))
        column = NSTableColumn.alloc().initWithIdentifier_("pane")
        column.setWidth_(SIDEBAR_WIDTH - 24)
        table.addTableColumn_(column)
        table.setHeaderView_(None)
        table.setStyle_(NSTableViewStyleSourceList)
        table.setRowHeight_(28)
        table.setAllowsEmptySelection_(False)  # a click below the rows keeps the pane
        table.setDataSource_(self)
        table.setDelegate_(self)
        scroll = NSScrollView.alloc().initWithFrame_(NSMakeRect(0, 0, SIDEBAR_WIDTH, BODY_HEIGHT))
        scroll.setDocumentView_(table)
        scroll.setDrawsBackground_(False)
        scroll.setHasVerticalScroller_(False)
        root.addSubview_(scroll)
        self.sidebar = table

        self.pane_host = NSView.alloc().initWithFrame_(
            NSMakeRect(SIDEBAR_WIDTH, 0, WINDOW_WIDTH - SIDEBAR_WIDTH, BODY_HEIGHT)
        )
        root.addSubview_(self.pane_host)
        window.setContentView_(root)
        self._panes = {
            PROVIDER: self._build_provider_pane(),
            HOTKEYS: self._build_hotkeys_pane(),
            OVERLAY: self._build_overlay_pane(),
            SHOW_WORK: self._build_show_work_pane(),
        }
        table.reloadData()
        table.selectRowIndexes_byExtendingSelection_(_index_set(PROVIDER), False)
        self._show_pane(PROVIDER)
        window.center()
        return window

    @objc.python_method
    def provider_pane(self) -> NSView:
        """A Provider pane of its own, for onboarding step 2 (no window)."""
        self.settings = store.load_settings()  # Settings may have changed it meanwhile
        pane = self._build_provider_pane(heading_text=None)
        self._show_provider(self.settings.provider)
        self._shown_panel.refresh(self.settings)
        return pane

    @objc.python_method
    def _show_pane(self, index: int) -> None:
        pane = self._panes[index]
        if self._current is pane:
            return
        if self._current is not None:
            self._current.removeFromSuperview()
        pane.setTranslatesAutoresizingMaskIntoConstraints_(False)
        self.pane_host.addSubview_(pane)
        for constraint in (
            pane.topAnchor().constraintEqualToAnchor_(self.pane_host.topAnchor()),
            pane.leadingAnchor().constraintEqualToAnchor_(self.pane_host.leadingAnchor()),
        ):
            constraint.setActive_(True)
        self._current = pane
        self.current_pane = index

    # NSWindowDelegate

    @entry_point
    def windowWillClose_(self, _notification):
        # Closing (red button, ⌘W) does not end editing: a pasted key whose field
        # still has focus would never be saved. Ending editing fires its action.
        self.window.makeFirstResponder_(None)
        self.work_color.deactivate()  # the colour panel goes with the window

    # NSTableViewDataSource / NSTableViewDelegate

    def numberOfRowsInTableView_(self, _table):
        return len(PANES)

    def tableView_objectValueForTableColumn_row_(self, _table, _column, row):
        return PANES[row]

    def tableView_viewForTableColumn_row_(self, table, _column, row):
        # View-based rows: a cell-based row draws its text at the top of the 28 pt
        # row, above the middle of the selection highlight. Here the label is
        # centred on the row.
        cell = table.makeViewWithIdentifier_owner_(SIDEBAR_CELL, self)
        if cell is None:
            cell = NSTableCellView.alloc().initWithFrame_(NSMakeRect(0, 0, SIDEBAR_WIDTH, 28))
            cell.setIdentifier_(SIDEBAR_CELL)
            field = _label("")
            field.setLineBreakMode_(NSLineBreakByTruncatingTail)
            field.setTranslatesAutoresizingMaskIntoConstraints_(False)
            cell.addSubview_(field)
            cell.setTextField_(field)
            for constraint in (
                field.leadingAnchor().constraintEqualToAnchor_constant_(cell.leadingAnchor(), 2),
                field.trailingAnchor().constraintLessThanOrEqualToAnchor_(cell.trailingAnchor()),
                field.centerYAnchor().constraintEqualToAnchor_(cell.centerYAnchor()),
            ):
                constraint.setActive_(True)
        cell.textField().setStringValue_(PANES[row])
        return cell

    def tableView_shouldEditTableColumn_row_(self, _table, _column, _row):
        return False

    @entry_point
    def tableViewSelectionDidChange_(self, _notification):
        row = self.sidebar.selectedRow()
        if 0 <= row < len(PANES):
            self._show_pane(int(row))

    @objc.python_method
    def _load_values(self) -> None:
        s = self.settings
        self._show_provider(s.provider)
        self._shown_panel.refresh(s)
        self.speed_slider.setIntValue_(s.double_tap_ms)
        self.speed_value.setStringValue_(f"{s.double_tap_ms} ms")
        for index, (mode, _title) in enumerate(DISPLAY_CHOICES):
            if mode == s.display_mode:
                self.display_popup.selectItemAtIndex_(index)
        self._refresh_login_item()
        self.size_popup.selectItemAtIndex_(store.TEXT_SIZES.index(s.answer_text_size))
        self.keep_switch.setState_(
            NSControlStateValueOn if s.keep_answers_visible else NSControlStateValueOff
        )
        self.preview.set_text_size(s.answer_text_size or PREVIEW_MATCH_SIZE)
        self._refresh_questions()
        self.work_switch.setState_(NSControlStateValueOn if s.show_work else NSControlStateValueOff)
        r, g, b = (c / 255 for c in s.work_rgb)
        self.work_color.setColor_(NSColor.colorWithSRGBRed_green_blue_alpha_(r, g, b, 1.0))
        self.work_box.set_box(s.work_box)
        self.work_box.set_rgb(s.work_rgb)
        self.work_box.set_enabled(s.show_work)

    @objc.python_method
    def _save(self, **changes) -> None:
        """Fresh load, change, atomic save, tell the app."""
        settings = store.load_settings()
        for name, value in changes.items():
            setattr(settings, name, value)
        try:
            store.save_settings(settings)
        except OSError:
            log.exception("Could not save settings")
        self.settings = settings
        self.on_change()

    # --- Provider pane --------------------------------------------------------

    @objc.python_method
    def _build_provider_pane(self, *, heading_text: str | None = "AI provider") -> NSView:
        heading = _heading(heading_text or "")
        subtitle = _label(
            "Pick who answers your screenshots. Claude is the default.", 12, secondary=True
        )
        segments = NSSegmentedControl.segmentedControlWithLabels_trackingMode_target_action_(
            [PROVIDERS[pid].name for pid in SEGMENTS],
            0,
            self,
            "providerChosen:",
        )
        segments.setSegmentStyle_(NSSegmentStyleRounded)
        for index, pid in enumerate(SEGMENTS):
            segments.setWidth_forSegment_(120, index)
            if pid not in self.key_caches:
                segments.setEnabled_forSegment_(False, index)
                segments.setToolTip_forSegment_("Arrives in a later version.", index)
        self.provider_segments = segments
        for pid, cache in self.key_caches.items():
            if PROVIDERS[pid].key_account is None:
                panel = ChatGPTPanel.alloc().init()
                panel.configure(
                    spec=PROVIDERS[pid],
                    credential=cache,
                    save=self._save,
                    on_change=self.on_change,
                    on_key_known=self.on_key_known,
                )
            else:
                panel = KeyProviderPanel.alloc().init()
                panel.configure(
                    spec=PROVIDERS[pid],
                    key_cache=cache,
                    save=self._save,
                    on_change=self.on_change,
                    on_key_known=self.on_key_known,
                )
            self.panels[pid] = panel
        self.panel_host = _stack([], vertical=True, spacing=0, align=NSLayoutAttributeLeading)
        footer = _wrapping(
            "Screenshots go only to the provider you pick and are never saved to disk.",
            CONTENT_WIDTH,
        )
        if not heading_text:  # onboarding has its own heading
            return _pane([subtitle, segments, self.panel_host, footer])
        return _pane(
            [heading, subtitle, segments, self.panel_host, footer],
            spacing_after={heading: 8},
        )

    @objc.python_method
    def _show_provider(self, pid: str) -> None:
        panel = self.panels.get(pid) or self.panels["claude"]
        self.provider_segments.setSelectedSegment_(SEGMENTS.index(panel.spec.id))
        if self._shown_panel is panel:
            return
        for view in list(self.panel_host.arrangedSubviews()):
            self.panel_host.removeView_(view)
        self.panel_host.addArrangedSubview_(panel.view)
        self._shown_panel = panel

    @entry_point
    def providerChosen_(self, sender):
        pid = SEGMENTS[int(sender.selectedSegment())]
        self.settings = store.load_settings()  # another window may have changed it
        if pid not in self.panels:
            self._show_provider(self.settings.provider)
            return
        self._show_provider(pid)
        self.panels[pid].refresh(self.settings)
        if pid != self.settings.provider:
            log.info("Provider set to %s", PROVIDERS[pid].name)
            self._save(provider=pid)

    # --- Hotkeys & capture pane ---------------------------------------------

    @objc.python_method
    def _build_hotkeys_pane(self) -> NSView:
        self.speed_slider = NSSlider.sliderWithValue_minValue_maxValue_target_action_(
            300, store.DOUBLE_TAP_RANGE[0], store.DOUBLE_TAP_RANGE[1], self, "speedChanged:"
        )
        self.speed_slider.setContinuous_(True)
        _fixed_width(self.speed_slider, 180)
        self.speed_value = _label("300 ms")
        _fixed_width(self.speed_value, 52)
        speed = _stack(
            [
                _label("Fast", 12, secondary=True),
                self.speed_slider,
                _label("Slow", 12, secondary=True),
                self.speed_value,
            ],
            vertical=False,
            spacing=8,
        )
        speed_controls = _stack(
            [speed, _wrapping(SPEED_NOTE, 307)],
            vertical=True,
            spacing=6,
            align=NSLayoutAttributeLeading,
        )
        hotkeys = _card(
            [
                _row(
                    "Capture & solve",
                    _keycaps("⌃", "Double-tap Control"),
                    width=PANE_LABEL_WIDTH,
                    centered=True,
                ),
                _row(
                    "Show / hide",
                    _keycaps("⌘", "Double-tap Command"),
                    width=PANE_LABEL_WIDTH,
                    centered=True,
                ),
                _row("Double-tap speed", speed_controls, width=PANE_LABEL_WIDTH),
            ]
        )

        self.display_popup = NSPopUpButton.alloc().initWithFrame_pullsDown_(
            NSMakeRect(0, 0, CONTROL_WIDTH, 26), False
        )
        for mode, title in DISPLAY_CHOICES:
            self.display_popup.addItemWithTitle_(title)
            self.display_popup.lastItem().setRepresentedObject_(mode)
        self.display_popup.setTarget_(self)
        self.display_popup.setAction_("displayChosen:")
        _fixed_width(self.display_popup, CONTROL_WIDTH)

        self.login_switch = _switch(self, "loginToggled:")
        self.login_note = _label("", 12, secondary=True)
        login = _stack([self.login_switch, self.login_note], vertical=False, spacing=10)

        self.questions_switch = _switch(self, "questionsToggled:")
        self.folder_label = _label(NO_FOLDER, 12, secondary=True)
        self.folder_label.setLineBreakMode_(NSLineBreakByTruncatingMiddle)
        _fixed_width(self.folder_label, 168)
        self.folder_button = NSButton.buttonWithTitle_target_action_(
            "Choose…", self, "chooseFolder:"
        )
        questions = _stack(
            [self.questions_switch, self.folder_label, self.folder_button],
            vertical=False,
            spacing=10,
        )
        capture = _card(
            [
                _row("Screen", self.display_popup, width=PANE_LABEL_WIDTH, centered=True),
                _row("Launch at login", login, width=PANE_LABEL_WIDTH, centered=True),
                _row("Save questions", questions, width=PANE_LABEL_WIDTH, centered=True),
            ]
        )
        title_hotkeys, title_capture = _heading("Hotkeys"), _heading("Capture")
        return _pane([title_hotkeys, hotkeys, title_capture, capture])

    @entry_point
    def speedChanged_(self, sender):
        ms = int(round(sender.intValue() / 10) * 10)  # 10 ms steps
        self.speed_value.setStringValue_(f"{ms} ms")
        if ms != self.settings.double_tap_ms:
            self._save(double_tap_ms=ms)

    @entry_point
    def displayChosen_(self, sender):
        mode = str(sender.selectedItem().representedObject())
        self._save(display_mode=mode)
        log.info("Capture display: %s", mode)

    @objc.python_method
    def _service(self):
        if self._login_service is not None:
            return self._login_service
        try:
            from ServiceManagement import SMAppService

            return SMAppService.mainAppService()
        except Exception:
            log.exception("SMAppService is not available")
            return None

    @objc.python_method
    def _refresh_login_item(self) -> None:
        service = self._service()
        status = int(service.status()) if service is not None else None
        on = status == 1
        self.login_switch.setState_(NSControlStateValueOn if on else NSControlStateValueOff)
        self.login_switch.setEnabled_(service is not None)
        self.login_note.setStringValue_(login_item_note(status))

    @entry_point
    def loginToggled_(self, sender):
        service = self._service()
        if service is None:
            self._refresh_login_item()
            return
        on = sender.state() == NSControlStateValueOn
        ok, error = (
            service.registerAndReturnError_(None) if on else service.unregisterAndReturnError_(None)
        )
        log.info("Launch at login %s: %s", "on" if on else "off", "ok" if ok else error)
        self._refresh_login_item()
        if not ok and error is not None and not self.login_note.stringValue():
            self.login_note.setStringValue_(str(error.localizedDescription()))

    # --- Overlay pane -----------------------------------------------------------

    @objc.python_method
    def _build_overlay_pane(self) -> NSView:
        self.size_popup = NSPopUpButton.alloc().initWithFrame_pullsDown_(
            NSMakeRect(0, 0, CONTROL_WIDTH, 26), False
        )
        for size in store.TEXT_SIZES:
            self.size_popup.addItemWithTitle_(text_size_title(size))
            self.size_popup.lastItem().setRepresentedObject_(size)
        self.size_popup.setTarget_(self)
        self.size_popup.setAction_("textSizeChosen:")
        _fixed_width(self.size_popup, CONTROL_WIDTH)

        self.keep_switch = _switch(self, "keepVisibleToggled:")
        keep = _stack(
            [
                self.keep_switch,
                _label("Keep answers showing after a new capture", 12, secondary=True),
            ],
            vertical=False,
            spacing=10,
        )
        style = _card(
            [
                _row("Text size", self.size_popup, width=PANE_LABEL_WIDTH, centered=True),
                _row("Keep visible", keep, width=PANE_LABEL_WIDTH, centered=True),
            ]
        )
        self.preview = AnswerPreviewView.alloc().initWithFrame_(NSMakeRect(0, 0, CONTENT_WIDTH, 68))
        _fixed_size(self.preview, CONTENT_WIDTH, 68)
        return _pane([_heading("Answer style"), style, _heading("Preview"), self.preview])

    @entry_point
    def textSizeChosen_(self, sender):
        size = int(sender.selectedItem().representedObject())
        self.preview.set_text_size(size or PREVIEW_MATCH_SIZE)
        self._save(answer_text_size=size)
        log.info("Answer text size: %s", text_size_title(size))

    @entry_point
    def keepVisibleToggled_(self, sender):
        self._save(keep_answers_visible=sender.state() == NSControlStateValueOn)

    # --- Save questions (Hotkeys & capture pane) ----------------------------------

    @objc.python_method
    def _refresh_questions(self) -> None:
        s = self.settings
        on = s.save_questions and bool(s.save_questions_folder)
        self.questions_switch.setState_(NSControlStateValueOn if on else NSControlStateValueOff)
        self.folder_label.setStringValue_(folder_title(s.save_questions_folder))
        self.folder_label.setToolTip_(s.save_questions_folder or None)

    @objc.python_method
    def _ask_for_folder(self, done: Callable[[str | None], None]) -> None:
        """A folder chooser as a sheet on the Settings window; done(path or None)."""
        if self._choose_folder is not None:
            self._choose_folder(done)
            return
        panel = NSOpenPanel.openPanel()
        panel.setCanChooseDirectories_(True)
        panel.setCanChooseFiles_(False)
        panel.setCanCreateDirectories_(True)
        panel.setAllowsMultipleSelection_(False)
        panel.setPrompt_("Choose")
        panel.setMessage_("Peek saves a picture of each question in this folder.")
        current = self.settings.save_questions_folder
        if current and os.path.isdir(current):
            panel.setDirectoryURL_(NSURL.fileURLWithPath_(current))

        def finished(response) -> None:
            try:
                url = panel.URL() if response == NSModalResponseOK else None
                done(str(url.path()) if url is not None else None)
            except Exception:
                log.exception("Choosing the questions folder failed")

        panel.beginSheetModalForWindow_completionHandler_(self.window, finished)

    @entry_point
    def questionsToggled_(self, sender):
        self.settings = store.load_settings()
        if sender.state() != NSControlStateValueOn:
            self._save(save_questions=False)
            log.info("Save questions: off")
            self._refresh_questions()
            return
        folder = self.settings.save_questions_folder
        if folder and os.path.isdir(folder):
            self._save(save_questions=True)
            log.info("Save questions: on")
            self._refresh_questions()
            return

        def chosen(path: str | None) -> None:
            if path:
                self._save(save_questions=True, save_questions_folder=path)
                log.info("Save questions: on (folder chosen)")
            self._refresh_questions()  # cancelled: the switch goes back off

        self._ask_for_folder(chosen)

    @entry_point
    def chooseFolder_(self, _sender):
        def chosen(path: str | None) -> None:
            if path:
                # Choosing a folder means saving there: the switch turns on too.
                self._save(save_questions=True, save_questions_folder=path)
                log.info("Save questions: folder changed")
            self._refresh_questions()

        self._ask_for_folder(chosen)

    # --- Show work pane ------------------------------------------------------------

    @objc.python_method
    def _build_show_work_pane(self) -> NSView:
        self.work_switch = _switch(self, "showWorkToggled:")
        switch_row = _stack(
            [self.work_switch, _wrapping(WORK_NOTE, 260)], vertical=False, spacing=10
        )
        self.work_color = NSColorWell.alloc().initWithFrame_(NSMakeRect(0, 0, 44, 24))
        _fixed_size(self.work_color, 44, 24)
        if hasattr(self.work_color, "setSupportsAlpha_"):
            self.work_color.setSupportsAlpha_(False)
        self.work_color.setTarget_(self)
        self.work_color.setAction_("workColorChosen:")
        color_row = _stack(
            [self.work_color, _label("Yellow by default", 12, secondary=True)],
            vertical=False,
            spacing=10,
        )
        card = _card(
            [
                _row("Show work", switch_row, width=PANE_LABEL_WIDTH, centered=True),
                _row("Text colour", color_row, width=PANE_LABEL_WIDTH, centered=True),
            ]
        )
        self.work_box = WorkBoxView.alloc().initWithFrame_(
            NSMakeRect(0, 0, CONTENT_WIDTH, WORK_BOX_HEIGHT)
        )
        _fixed_size(self.work_box, CONTENT_WIDTH, WORK_BOX_HEIGHT)
        self.work_box.on_change = self._work_box_moved
        hint = _label(WORK_BOX_HINT, 12, secondary=True)
        where = _heading("Where the working shows")
        return _pane(
            [_heading("Show work"), card, where, self.work_box, hint],
            spacing_after={self.work_box: 6},
        )

    @entry_point
    def showWorkToggled_(self, sender):
        on = sender.state() == NSControlStateValueOn
        self.work_box.set_enabled(on)
        self._save(show_work=on)
        log.info("Show work: %s", "on" if on else "off")

    @entry_point
    def workColorChosen_(self, sender):
        color = sender.color().colorUsingColorSpace_(NSColorSpace.sRGBColorSpace())
        if color is None:
            return
        rgb = [
            round(min(max(float(c), 0.0), 1.0) * 255)
            for c in (color.redComponent(), color.greenComponent(), color.blueComponent())
        ]
        self.work_box.set_rgb(rgb)
        if rgb != list(self.settings.work_rgb):
            self._save(work_rgb=rgb)

    @objc.python_method
    def _work_box_moved(self, box) -> None:
        self._save(work_box=store.valid_work_box(box))


def _index_set(index: int):
    from Foundation import NSIndexSet

    return NSIndexSet.indexSetWithIndex_(index)
