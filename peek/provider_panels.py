"""Settings → Provider panels for API-key providers (Figma 3:101 Claude, 3:205 Gemini).

Each panel: an API key field (secure; the stored key shown masked as the
placeholder), Test connection + status, and the Model pop-up. Gemini adds the
"Get a free key in Google AI Studio" link, loads its models from the account,
and shows the free-tier warning under the card.

Keychain and network work run on background threads; results come back with
AppHelper.callAfter. Main thread otherwise.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable

import objc
from AppKit import (
    NSURL,
    NSAttributedString,
    NSBezelStyleRounded,
    NSBezierPath,
    NSButton,
    NSColor,
    NSCursor,
    NSFont,
    NSFontAttributeName,
    NSForegroundColorAttributeName,
    NSImage,
    NSImageView,
    NSInsetRect,
    NSLayoutAttributeLeading,
    NSLayoutAttributeTop,
    NSLineBreakByWordWrapping,
    NSMakeRect,
    NSObject,
    NSPopUpButton,
    NSSecureTextField,
    NSStackView,
    NSTextAlignmentRight,
    NSTextField,
    NSUserInterfaceLayoutOrientationHorizontal,
    NSUserInterfaceLayoutOrientationVertical,
    NSView,
    NSWorkspace,
)
from Foundation import NSEdgeInsetsMake
from PyObjCTools import AppHelper

from peek import store
from peek.log import entry_point
from peek.providers.base import ModelInfo, ProviderError
from peek.providers.catalog import ProviderSpec
from peek.status import DotColor
from peek.ui_common import CardView, StatusDotView

log = logging.getLogger(__name__)

LABEL_WIDTH = 92
CONTROL_WIDTH = 260
CONTENT_WIDTH = 475
AI_STUDIO_KEYS = "https://aistudio.google.com/apikey"
GEMINI_FREE_TIER_NOTE = (
    "On the free tier, Google may use your requests to improve its products. "
    "Avoid capturing private information."
)
MODEL_LIST_NOTE = {"gemini": "List loaded from your Google AI Studio account."}


def label(text: str, size: float = 13, *, secondary: bool = False) -> NSTextField:
    field = NSTextField.labelWithString_(text)
    field.setFont_(NSFont.systemFontOfSize_(size))
    field.setTextColor_(NSColor.secondaryLabelColor() if secondary else NSColor.labelColor())
    return field


def fixed_width(view: NSView, width: float) -> NSView:
    view.setTranslatesAutoresizingMaskIntoConstraints_(False)
    view.widthAnchor().constraintEqualToConstant_(width).setActive_(True)
    return view


def stack(views, *, vertical: bool, spacing: float, align=None) -> NSStackView:
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


def row(label_text: str, controls: NSView) -> NSStackView:
    lbl = label(label_text)
    lbl.setAlignment_(NSTextAlignmentRight)
    fixed_width(lbl, LABEL_WIDTH)
    # Figma: the label sits 4 pt down so it centres on the 28 pt control beside it.
    holder = stack([lbl], vertical=True, spacing=0)
    holder.setEdgeInsets_(NSEdgeInsetsMake(4, 0, 0, 0))
    return stack([holder, controls], vertical=False, spacing=14, align=NSLayoutAttributeTop)


class LinkButton(NSButton):
    """A borderless button that looks like a link (accent colour, pointer cursor)."""

    def resetCursorRects(self):
        self.addCursorRect_cursor_(self.bounds(), NSCursor.pointingHandCursor())


def link_button(title: str, target, action: str) -> NSButton:
    button = LinkButton.buttonWithTitle_target_action_(title, target, action)
    button.setBordered_(False)
    button.setAttributedTitle_(
        NSAttributedString.alloc().initWithString_attributes_(
            title,
            {
                NSFontAttributeName: NSFont.systemFontOfSize_(12),
                NSForegroundColorAttributeName: NSColor.linkColor(),
            },
        )
    )
    return button


class WarningNoteView(NSView):
    """The Figma warning note: a tinted rounded box with a warning icon."""

    TINT = (0xF5 / 255, 0xA6 / 255, 0x23 / 255)  # status/warning

    def drawRect_(self, _rect):
        path = NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
            NSInsetRect(self.bounds(), 0.5, 0.5), 8, 8
        )
        NSColor.colorWithSRGBRed_green_blue_alpha_(*self.TINT, 0.16).setFill()
        path.fill()

    def isOpaque(self):
        return False


def warning_note(text: str, width: float) -> NSView:
    note = WarningNoteView.alloc().initWithFrame_(NSMakeRect(0, 0, width, 50))
    icon = NSImageView.imageViewWithImage_(
        NSImage.imageWithSystemSymbolName_accessibilityDescription_(
            "exclamationmark.triangle.fill", "Warning"
        )
    )
    icon.setContentTintColor_(NSColor.systemOrangeColor())
    body = label(text, 12)
    body.setLineBreakMode_(NSLineBreakByWordWrapping)
    body.setPreferredMaxLayoutWidth_(width - 60)
    body.cell().setWraps_(True)
    content = stack([icon, body], vertical=False, spacing=10, align=NSLayoutAttributeTop)
    content.setTranslatesAutoresizingMaskIntoConstraints_(False)
    note.addSubview_(content)
    for c in (
        content.topAnchor().constraintEqualToAnchor_constant_(note.topAnchor(), 10),
        content.leadingAnchor().constraintEqualToAnchor_constant_(note.leadingAnchor(), 14),
        note.bottomAnchor().constraintEqualToAnchor_constant_(content.bottomAnchor(), 10),
        note.trailingAnchor().constraintGreaterThanOrEqualToAnchor_constant_(
            content.trailingAnchor(), 14
        ),
    ):
        c.setActive_(True)
    fixed_width(note, width)
    return note


class KeyProviderPanel(NSObject):
    """One API-key provider's panel. `view` is the card plus any note under it."""

    @objc.python_method
    def configure(
        self,
        *,
        spec: ProviderSpec,
        key_cache: store.SecretCache,
        save: Callable[..., None],
        on_change: Callable[[], None],
        on_key_known: Callable[[bool | None, str], None],
    ) -> None:
        self.spec = spec
        self.key_cache = key_cache
        self._save = save
        self.on_change = on_change
        self.on_key_known = on_key_known
        self._test_seq = 0  # bumped by key saves and model changes
        self._test_again = False
        self._testing = False
        self._models: list[ModelInfo] = list(spec.models)
        self._models_loaded = False
        self._account_list = False
        self.view = self._build()
        # The saved (or default) model from the start, not the list's first entry.
        saved = store.load_settings().model_for(spec.id)
        self._select_model(saved or spec.default_model)

    # --- building -----------------------------------------------------------

    @objc.python_method
    def _build(self) -> NSView:
        spec = self.spec
        self.key_field = NSSecureTextField.alloc().initWithFrame_(
            NSMakeRect(0, 0, CONTROL_WIDTH, 22)
        )
        self.key_field.setPlaceholderString_(spec.key_placeholder)
        self.key_field.setTarget_(self)
        self.key_field.setAction_("keyEntered:")
        self.key_field.cell().setSendsActionOnEndEditing_(True)
        self.key_field.setAccessibilityLabel_(f"{spec.name} API key")
        fixed_width(self.key_field, CONTROL_WIDTH)
        under_key = (
            link_button("Get a free key in Google AI Studio ↗", self, "openKeyPage:")
            if spec.id == "gemini"
            else label("Stored in your macOS Keychain.", 12, secondary=True)
        )
        key_controls = stack(
            [self.key_field, under_key], vertical=True, spacing=6, align=NSLayoutAttributeLeading
        )

        self.test_button = NSButton.buttonWithTitle_target_action_(
            "Test connection", self, "testConnection:"
        )
        self.test_button.setBezelStyle_(NSBezelStyleRounded)
        self.status_dot = StatusDotView.alloc().initWithFrame_(NSMakeRect(0, 0, 8, 8))
        self.status_dot.setTranslatesAutoresizingMaskIntoConstraints_(False)
        self.status_dot.widthAnchor().constraintEqualToConstant_(8).setActive_(True)
        self.status_dot.heightAnchor().constraintEqualToConstant_(8).setActive_(True)
        self.status_label = label("Not tested", 12, secondary=True)
        status = stack([self.status_dot, self.status_label], vertical=False, spacing=6)
        test_controls = stack([self.test_button, status], vertical=False, spacing=10)

        self.model_popup = NSPopUpButton.alloc().initWithFrame_pullsDown_(
            NSMakeRect(0, 0, CONTROL_WIDTH, 26), False
        )
        self.model_popup.setTarget_(self)
        self.model_popup.setAction_("modelChosen:")
        fixed_width(self.model_popup, CONTROL_WIDTH)
        self.model_note = label("", 12, secondary=True)
        self._fill_models()
        model_controls = stack(
            [self.model_popup, self.model_note],
            vertical=True,
            spacing=6,
            align=NSLayoutAttributeLeading,
        )

        card = CardView.alloc().initWithFrame_(NSMakeRect(0, 0, CONTENT_WIDTH, 100))
        card.set_content(
            stack(
                [
                    row("API key", key_controls),
                    row(" ", test_controls),
                    row("Model", model_controls),
                ],
                vertical=True,
                spacing=14,
                align=NSLayoutAttributeLeading,
            )
        )
        fixed_width(card, CONTENT_WIDTH)
        if spec.id != "gemini":
            return card
        return stack(
            [card, warning_note(GEMINI_FREE_TIER_NOTE, CONTENT_WIDTH)],
            vertical=True,
            spacing=14,
            align=NSLayoutAttributeLeading,
        )

    # --- values -------------------------------------------------------------

    @objc.python_method
    def refresh(self, settings: store.Settings) -> None:
        """Show the saved model and the stored key (masked)."""
        self._select_model(settings.model_for(self.spec.id) or self.spec.default_model)
        self._refresh_key_placeholder()

    @objc.python_method
    def _fill_models(self) -> None:
        current = self.selected_model() if self.model_popup.numberOfItems() else None
        self.model_popup.removeAllItems()
        for m in self._models:
            self.model_popup.addItemWithTitle_(m.name)
            self.model_popup.lastItem().setRepresentedObject_(m.id)
            if m.note:
                self.model_popup.lastItem().setToolTip_(m.note)
        if current:
            self._select_model(current)

    @objc.python_method
    def _select_model(self, model_id: str) -> None:
        for index, m in enumerate(self._models):
            if m.id == model_id:
                self.model_popup.selectItemAtIndex_(index)
                if self.spec.fixed_models:
                    self.model_note.setStringValue_(m.note)
                else:  # only true once the account's own list has arrived
                    note = MODEL_LIST_NOTE.get(self.spec.id, "") if self._account_list else ""
                    self.model_note.setStringValue_(note)
                return
        if self.spec.accepts_model(model_id):  # saved, but not (yet) in the list
            self._models.append(ModelInfo(model_id, self.spec.display_name(model_id)))
            self._fill_models()
            self._select_model(model_id)
        elif model_id != self.spec.default_model:
            self._select_model(self.spec.default_model)

    @objc.python_method
    def selected_model(self) -> str:
        item = self.model_popup.selectedItem()
        return str(item.representedObject()) if item is not None else self.spec.default_model

    @entry_point
    def modelChosen_(self, _sender):
        model = self.selected_model()
        self._select_model(model)
        models = dict(store.load_settings().models)
        models[self.spec.id] = model
        log.info("%s model set to %s", self.spec.name, model)
        self._test_seq += 1
        self._set_status("Not tested", DotColor.NEUTRAL)
        self._save(models=models)

    @objc.python_method
    def _load_models(self) -> None:
        """Gemini: replace the built-in list with the account's (once per window)."""
        if self.spec.fixed_models or self._models_loaded:
            return
        self._models_loaded = True
        spec, cache = self.spec, self.key_cache

        def work() -> None:
            with objc.autorelease_pool():
                try:
                    key = cache.get()
                    models = spec.create(key).list_models() if key else []
                except Exception as exc:
                    log.warning("Could not list %s models: %s", spec.name, exc)
                    models = []
            AppHelper.callAfter(entry_point(self._models_arrived), models)

        threading.Thread(target=work, name="list-models", daemon=True).start()

    @objc.python_method
    def _models_arrived(self, models: list[ModelInfo]) -> None:
        if not models:
            self._models_loaded = False  # try again next time the window opens
            return
        ids = [m.id for m in models]  # the account's own list (before any additions)
        self._models = list(models)
        self._account_list = True
        self._fill_models()
        log.info("%s: %d models from the account", self.spec.name, len(models))
        settings = store.load_settings()
        if settings.model_lists.get(self.spec.id) != ids:  # Hard mode picks from these
            self._save(model_lists={**settings.model_lists, self.spec.id: ids})

    # --- API key ------------------------------------------------------------

    @entry_point
    def openKeyPage_(self, _sender):
        NSWorkspace.sharedWorkspace().openURL_(NSURL.URLWithString_(AI_STUDIO_KEYS))

    @objc.python_method
    def _refresh_key_placeholder(self) -> None:
        cache = self.key_cache

        def work() -> None:
            with objc.autorelease_pool():
                try:
                    key = cache.get()
                except Exception:
                    log.exception("Could not read the API key from the Keychain")
                    key = None
            AppHelper.callAfter(entry_point(self._show_key_placeholder), key)

        threading.Thread(target=work, name="keychain-read", daemon=True).start()

    @objc.python_method
    def _show_key_placeholder(self, key: str | None) -> None:
        self.key_field.setPlaceholderString_(
            store.mask_key(key) if key else self.spec.key_placeholder
        )
        if not key and not self._testing:
            self._set_status("No API key yet", DotColor.NEUTRAL)
        if key:
            self.on_key_known(True, self.spec.id)  # the app may have failed to read it
            self._load_models()

    @entry_point
    def keyEntered_(self, _sender):
        key = "".join(str(self.key_field.stringValue()).split())  # pasted line breaks
        if not key:
            return  # leaving the field empty never deletes a stored key
        self.key_field.setStringValue_("")
        prefix = self.spec.key_prefix
        if not key.startswith(prefix):
            self._set_status(
                f"That doesn't look like a {self.spec.name} key ({prefix}…)", DotColor.DANGER
            )
            return
        self._set_status("Saving…", DotColor.NEUTRAL)
        cache = self.key_cache

        def work() -> None:
            with objc.autorelease_pool():
                try:
                    cache.set(key)
                    ok = True
                except Exception:
                    log.exception("Could not save the API key to the Keychain")
                    ok = False
            AppHelper.callAfter(entry_point(self._key_saved), ok, key)

        threading.Thread(target=work, name="keychain-write", daemon=True).start()

    @objc.python_method
    def _key_saved(self, ok: bool, key: str) -> None:
        if not ok:
            self._set_status("Couldn't save to the Keychain", DotColor.DANGER)
            return
        log.info("%s API key saved to the Keychain", self.spec.name)
        self.key_field.setPlaceholderString_(store.mask_key(key))
        self._test_seq += 1
        self._models_loaded = False
        self._load_models()
        self.on_change()
        self.testConnection_(None)

    # --- test connection ----------------------------------------------------

    @entry_point
    def testConnection_(self, _sender):
        if self._testing:
            self._test_again = True  # re-run with the new key/model when this one ends
            return
        self._testing = True
        self._test_again = False
        seq = self._test_seq
        self.test_button.setEnabled_(False)
        self._set_status("Testing…", DotColor.NEUTRAL)
        model, spec, cache = self.selected_model(), self.spec, self.key_cache

        def work() -> None:
            with objc.autorelease_pool():
                try:
                    key = cache.get()
                    if not key:
                        raise ProviderError("not_configured", "Add your API key first")
                    spec.create(key).test_connection(model)
                    outcome = ("Connected", DotColor.SUCCESS)
                except ProviderError as exc:
                    outcome = (str(exc), DotColor.DANGER)
                except Exception:
                    log.exception("Test connection failed")
                    outcome = ("Test failed; see the log", DotColor.DANGER)
            AppHelper.callAfter(entry_point(self._tested), seq, *outcome)

        threading.Thread(target=work, name="test-connection", daemon=True).start()

    @objc.python_method
    def _tested(self, seq: int, text: str, dot: DotColor) -> None:
        self._testing = False
        self.test_button.setEnabled_(True)
        if seq != self._test_seq or self._test_again:
            self.testConnection_(None)  # the key or model changed while testing
            return
        self._set_status(text, dot)
        log.info("%s test connection: %s", self.spec.name, text)

    @objc.python_method
    def _set_status(self, text: str, dot: DotColor) -> None:
        self.status_label.setStringValue_(text)
        self.status_dot.set_dot(dot)


# --- ChatGPT (Sign in with ChatGPT) ------------------------------------------------

CHATGPT_PITCH = "Use your ChatGPT Plus or Pro plan. No API key needed."
CHATGPT_HELPER = "Opens your browser to sign in with OpenAI."
CHATGPT_MODEL_NOTE = "Requests count against your ChatGPT plan’s usage limits."
PLAN_NOTICE_TITLE = "You’re using your ChatGPT plan"
PLAN_NOTICE_BODY = "Answers from Peek count against your ChatGPT plan’s usage limits."


class DarkButton(NSButton):
    """The Figma "Continue with ChatGPT" button: a filled rounded rectangle in the
    label colour with the window colour as text, so it inverts in dark mode."""

    def drawRect_(self, _rect):
        path = NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(self.bounds(), 8, 8)
        color = NSColor.labelColor()
        if not self.isEnabled():
            color = color.colorWithAlphaComponent_(0.35)
        elif self.isHighlighted():
            color = color.colorWithAlphaComponent_(0.8)
        color.setFill()
        path.fill()
        title = NSAttributedString.alloc().initWithString_attributes_(
            self.title(),
            {
                NSFontAttributeName: NSFont.boldSystemFontOfSize_(13),
                NSForegroundColorAttributeName: NSColor.windowBackgroundColor(),
            },
        )
        size, bounds = title.size(), self.bounds()
        title.drawAtPoint_(
            ((bounds.size.width - size.width) / 2, (bounds.size.height - size.height) / 2)
        )


class BadgeView(NSView):
    """ "Using ChatGPT plan": accent text on a 12% accent pill (Figma 3:156)."""

    def initWithFrame_(self, frame):
        self = objc.super(BadgeView, self).initWithFrame_(frame)
        if self is None:
            return None
        self._text = "Using ChatGPT plan"
        return self

    def _title(self):
        from AppKit import NSFontWeightMedium

        return NSAttributedString.alloc().initWithString_attributes_(
            self._text,
            {
                NSFontAttributeName: NSFont.systemFontOfSize_weight_(12, NSFontWeightMedium),
                NSForegroundColorAttributeName: NSColor.controlAccentColor(),
            },
        )

    def intrinsicContentSize(self):
        size = self._title().size()
        return (size.width + 16, size.height + 6)

    def drawRect_(self, _rect):
        bounds = self.bounds()
        radius = bounds.size.height / 2
        NSColor.controlAccentColor().colorWithAlphaComponent_(0.12).setFill()
        NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(bounds, radius, radius).fill()
        title = self._title()
        size = title.size()
        title.drawAtPoint_((8, (bounds.size.height - size.height) / 2))

    def isOpaque(self):
        return False


def _card_with(content: NSView, padding: float = 17) -> CardView:
    card = CardView.alloc().initWithFrame_(NSMakeRect(0, 0, CONTENT_WIDTH, 100))
    card.set_content(content, padding=padding)
    fixed_width(card, CONTENT_WIDTH)
    return card


class ChatGPTPanel(NSObject):
    """Signed out / signing in / signed in (with or without plan usage)."""

    @objc.python_method
    def configure(
        self,
        *,
        spec: ProviderSpec,
        credential,
        save: Callable[..., None],
        on_change: Callable[[], None],
        on_key_known: Callable[[bool | None, str], None],
        open_url: Callable[[str], None] | None = None,
        show_notice: Callable[[str, str], None] | None = None,
    ) -> None:
        self.spec = spec
        self.session = credential.session
        self._save = save
        self.on_change = on_change
        self.on_key_known = on_key_known
        self._open_url = open_url or _open_in_browser
        self._show_notice = show_notice or _alert
        self._cancel: threading.Event | None = None
        self._models: list[ModelInfo] = list(spec.models)
        self._signed_in = False
        self._plan = False
        self._email = ""
        self.view = stack([], vertical=True, spacing=0, align=NSLayoutAttributeLeading)
        self._build_signed_out()
        self._build_signed_in()
        self._show(self.signed_out_card)

    # --- building -----------------------------------------------------------

    @objc.python_method
    def _build_signed_out(self) -> None:
        self.continue_button = DarkButton.buttonWithTitle_target_action_(
            "Continue with ChatGPT", self, "continueWithChatGPT:"
        )
        self.continue_button.setBordered_(False)
        self.continue_button.setTranslatesAutoresizingMaskIntoConstraints_(False)
        self.continue_button.widthAnchor().constraintEqualToConstant_(180).setActive_(True)
        self.continue_button.heightAnchor().constraintEqualToConstant_(32).setActive_(True)
        self.cancel_button = NSButton.buttonWithTitle_target_action_(
            "Cancel", self, "cancelSignIn:"
        )
        self.cancel_button.setBezelStyle_(NSBezelStyleRounded)
        self.cancel_button.setHidden_(True)
        self.signin_status = label("", 12, secondary=True)
        self.signin_status.setLineBreakMode_(NSLineBreakByWordWrapping)
        self.signin_status.setPreferredMaxLayoutWidth_(CONTENT_WIDTH - 40)
        self.signin_status.setHidden_(True)
        self.other_account_button = NSButton.buttonWithTitle_target_action_(
            "Use a different account", self, "useDifferentAccount:"
        )
        self.other_account_button.setBezelStyle_(NSBezelStyleRounded)
        self.other_account_button.setHidden_(True)
        buttons = stack([self.continue_button, self.cancel_button], vertical=False, spacing=10)
        content = stack(
            [
                label(CHATGPT_PITCH),
                buttons,
                label(CHATGPT_HELPER, 12, secondary=True),
                self.signin_status,
                self.other_account_button,
            ],
            vertical=True,
            spacing=12,
            align=NSLayoutAttributeLeading,
        )
        self.signed_out_card = _card_with(content, padding=20)

    @objc.python_method
    def _build_signed_in(self) -> None:
        self.account_label = label("Signed in")
        self.badge = BadgeView.alloc().initWithFrame_(NSMakeRect(0, 0, 132, 21))
        self.manage_link = link_button("Manage usage ↗", self, "manageUsage:")
        self.plan_row = stack([self.badge, self.manage_link], vertical=False, spacing=10)
        self.plan_note = label("Plan usage isn't allowed yet.", 12, secondary=True)
        self.allow_button = NSButton.buttonWithTitle_target_action_(
            "Allow plan usage", self, "allowPlanUsage:"
        )
        self.allow_button.setBezelStyle_(NSBezelStyleRounded)
        self.declined_row = stack([self.plan_note, self.allow_button], vertical=False, spacing=10)
        self.sign_out_button = NSButton.buttonWithTitle_target_action_("Sign out", self, "signOut:")
        self.sign_out_button.setBezelStyle_(NSBezelStyleRounded)
        account = stack(
            [self.account_label, self.plan_row, self.declined_row, self.sign_out_button],
            vertical=True,
            spacing=8,
            align=NSLayoutAttributeLeading,
        )
        self.model_popup = NSPopUpButton.alloc().initWithFrame_pullsDown_(
            NSMakeRect(0, 0, CONTROL_WIDTH, 26), False
        )
        self.model_popup.setTarget_(self)
        self.model_popup.setAction_("modelChosen:")
        fixed_width(self.model_popup, CONTROL_WIDTH)
        self._fill_models()
        model = stack(
            [self.model_popup, label(CHATGPT_MODEL_NOTE, 12, secondary=True)],
            vertical=True,
            spacing=6,
            align=NSLayoutAttributeLeading,
        )
        self.signed_in_card = _card_with(
            stack(
                [row("Account", account), row("Model", model)],
                vertical=True,
                spacing=14,
                align=NSLayoutAttributeLeading,
            )
        )

    @objc.python_method
    def _show(self, card: NSView) -> None:
        for view in list(self.view.arrangedSubviews()):
            self.view.removeView_(view)
        self.view.addArrangedSubview_(card)

    # --- state ----------------------------------------------------------------

    @objc.python_method
    def refresh(self, settings: store.Settings) -> None:
        """Read the sign-in state off the main thread (Keychain)."""
        session = self.session

        def work() -> None:
            with objc.autorelease_pool():
                try:
                    signed_in, plan = session.signed_in(), session.plan_granted()
                    email = session.account().email
                except Exception:
                    log.exception("Could not read the ChatGPT sign-in")
                    signed_in, plan, email = False, False, ""
            AppHelper.callAfter(entry_point(self._state_known), signed_in, plan, email)

        threading.Thread(target=work, name="chatgpt-state", daemon=True).start()
        self._select_model(settings.model_for(self.spec.id) or self.spec.default_model)

    @objc.python_method
    def _state_known(self, signed_in: bool, plan: bool, email: str) -> None:
        self._signed_in, self._plan, self._email = signed_in, plan, email
        if not signed_in:
            self._show(self.signed_out_card)
            return
        self.account_label.setStringValue_(f"Signed in as {email}" if email else "Signed in")
        self.plan_row.setHidden_(not plan)
        self.declined_row.setHidden_(plan)
        self._show(self.signed_in_card)
        if plan:
            self._load_models()

    @objc.python_method
    def _set_signin_status(self, text: str) -> None:
        self.signin_status.setStringValue_(text)
        self.signin_status.setHidden_(not text)

    # --- sign in / out ------------------------------------------------------------

    @entry_point
    def continueWithChatGPT_(self, _sender):
        self._start_sign_in(consent=False)

    @entry_point
    def allowPlanUsage_(self, _sender):
        self._show(self.signed_out_card)
        self._start_sign_in(consent=True)

    @objc.python_method
    def _start_sign_in(self, *, consent: bool) -> None:
        if self._cancel is not None:
            return  # already waiting for the browser
        cancel = self._cancel = threading.Event()
        self.continue_button.setEnabled_(False)
        self.cancel_button.setHidden_(False)
        self._set_signin_status("Waiting for your browser…")
        session, opener = self.session, self._open_url

        def open_url(url: str) -> None:
            AppHelper.callAfter(opener, url)

        def work() -> None:
            from peek.providers.oauth_chatgpt import SignInError

            with objc.autorelease_pool():
                try:
                    session.sign_in(open_url, cancel=cancel, consent=consent)
                    outcome = None
                except SignInError as exc:
                    outcome = (exc.kind, str(exc))
                except ProviderError as exc:
                    outcome = (exc.kind, str(exc))
                except Exception:
                    log.exception("ChatGPT sign-in failed")
                    outcome = ("failed", "Sign-in failed; see the log")
                try:  # read here, not on the main thread (the session lock)
                    state = session.state()
                except Exception:
                    log.exception("Could not read the ChatGPT sign-in")
                    state = (False, False, "", True)
            AppHelper.callAfter(entry_point(self._sign_in_done), outcome, state)

        threading.Thread(target=work, name="chatgpt-sign-in", daemon=True).start()

    @entry_point
    def cancelSignIn_(self, _sender):
        if self._cancel is not None:
            self._cancel.set()

    @objc.python_method
    def _sign_in_done(self, outcome, state=(False, False, "", True)) -> None:
        self._cancel = None
        self.continue_button.setEnabled_(True)
        self.cancel_button.setHidden_(True)
        signed_in, plan, email, notice_shown = state
        if outcome is not None:
            kind, message = outcome
            self._set_signin_status("" if kind == "cancelled" else message)
            self.other_account_button.setHidden_(kind != "other_account")
            if signed_in:  # e.g. "Allow plan usage" was cancelled: still signed in
                self._state_known(signed_in, plan, email)
            return
        self._set_signin_status("")
        self.other_account_button.setHidden_(True)
        if plan and not notice_shown:
            self._show_notice(PLAN_NOTICE_TITLE, PLAN_NOTICE_BODY)
            threading.Thread(
                target=self.session.mark_plan_notice_shown, name="chatgpt-notice", daemon=True
            ).start()
        self.on_key_known(plan, self.spec.id)
        self.on_change()
        self._state_known(signed_in, plan, email)

    @entry_point
    def useDifferentAccount_(self, _sender):
        """Forget Peek's registration with the old account, then sign in afresh."""
        self.other_account_button.setHidden_(True)
        session = self.session

        def work() -> None:
            with objc.autorelease_pool():
                try:
                    session.forget_account()
                except Exception:
                    log.exception("Could not forget the ChatGPT account")
            AppHelper.callAfter(entry_point(self._start_fresh))

        threading.Thread(target=work, name="chatgpt-forget", daemon=True).start()

    @objc.python_method
    def _start_fresh(self) -> None:
        self._start_sign_in(consent=False)

    @entry_point
    def signOut_(self, _sender):
        self.sign_out_button.setEnabled_(False)
        session = self.session

        def work() -> None:
            with objc.autorelease_pool():
                try:
                    confirmed = session.sign_out()
                except Exception:
                    # The Keychain item could not be deleted: still signed in.
                    log.exception("ChatGPT sign-out failed")
                    confirmed = None
            AppHelper.callAfter(entry_point(self._signed_out), confirmed)

        threading.Thread(target=work, name="chatgpt-sign-out", daemon=True).start()

    @objc.python_method
    def _signed_out(self, confirmed: bool | None) -> None:
        self.sign_out_button.setEnabled_(True)
        if confirmed is None:
            self.account_label.setStringValue_(
                "Couldn't remove the sign-in from your Keychain; try again."
            )
            return
        self._signed_in = False
        self._show(self.signed_out_card)
        self._set_signin_status(
            ""
            if confirmed
            else "Signed out. OpenAI didn't confirm it; you can also disconnect Peek in "
            "ChatGPT → Settings → Security."
        )
        self.on_key_known(False, self.spec.id)
        self.on_change()

    @entry_point
    def manageUsage_(self, _sender):
        from peek.providers.oauth_chatgpt import USAGE_URL

        self._open_url(USAGE_URL)

    # --- models -------------------------------------------------------------------

    @objc.python_method
    def _fill_models(self) -> None:
        self.model_popup.removeAllItems()
        for m in self._models:
            self.model_popup.addItemWithTitle_(m.name)
            self.model_popup.lastItem().setRepresentedObject_(m.id)

    @objc.python_method
    def _select_model(self, model_id: str) -> None:
        for index, m in enumerate(self._models):
            if m.id == model_id:
                self.model_popup.selectItemAtIndex_(index)
                return
        listed = store.load_settings().model_lists.get(self.spec.id) or []
        if model_id and (model_id in listed or self.spec.accepts_model(model_id)):
            # Saved from the account's list earlier: show what captures will use.
            self._models.append(ModelInfo(model_id, self.spec.display_name(model_id)))
            self._fill_models()
            self._select_model(model_id)
        elif self._models:
            self.model_popup.selectItemAtIndex_(0)

    @objc.python_method
    def selected_model(self) -> str:
        item = self.model_popup.selectedItem()
        return str(item.representedObject()) if item is not None else self.spec.default_model

    @entry_point
    def modelChosen_(self, _sender):
        models = dict(store.load_settings().models)
        models[self.spec.id] = self.selected_model()
        self._save(models=models)

    @objc.python_method
    def _load_models(self) -> None:
        session = self.session

        def work() -> None:
            from peek.providers.chatgpt import ChatGPTProvider

            with objc.autorelease_pool():
                try:
                    models = ChatGPTProvider(session.access_token()).list_models()
                except Exception as exc:
                    log.warning("Could not list ChatGPT models: %s", exc)
                    models = []
            AppHelper.callAfter(entry_point(self._models_arrived), models)

        threading.Thread(target=work, name="chatgpt-models", daemon=True).start()

    @objc.python_method
    def _models_arrived(self, models: list[ModelInfo]) -> None:
        if not models:
            return
        settings = store.load_settings()
        saved = settings.model_for(self.spec.id)
        self._models = list(models)
        self._fill_models()
        ids = [m.id for m in models]
        changes = {}
        if saved not in ids:  # the account's first model (server order) by default
            changes["models"] = {**settings.models, self.spec.id: ids[0]}
            saved = ids[0]
        if settings.model_lists.get(self.spec.id) != ids:
            changes["model_lists"] = {**settings.model_lists, self.spec.id: ids}
        self._select_model(saved)
        if changes:
            self._save(**changes)


def _open_in_browser(url: str) -> None:
    NSWorkspace.sharedWorkspace().openURL_(NSURL.URLWithString_(url))


def _alert(title: str, body: str) -> None:
    from AppKit import NSAlert

    alert = NSAlert.alloc().init()
    alert.setMessageText_(title)
    alert.setInformativeText_(body)
    alert.addButtonWithTitle_("Got it")
    alert.runModal()
