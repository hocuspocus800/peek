# Peek

A macOS menu-bar app, written in Python. Double-tap **Control** to capture the
screen; an AI model finds every question on it and answers it. Double-tap
**Command** to show or hide the answers, drawn in a click-through overlay where
each answer belongs.

Requirements: macOS 14 or later (developed and tested on macOS 26.3, Apple
Silicon; an Intel build is made separately, see below).

## First run

Peek opens a **Welcome to Peek** window:

1. **Permissions.** Screen Recording (to take the screenshot you ask for) and
   Input Monitoring (to notice a double-tap on Control or Command). **Open System
   Settings** asks macOS and opens the right pane. macOS often applies a grant only
   after Peek restarts: use **Quit & Reopen** on the card, and the window comes
   back where you left off. **Continue** unlocks when both say Granted.
2. **Connect an AI provider.** Claude is preselected; pick ChatGPT or Gemini if
   you prefer. Then **Done**.

## Using Peek

- **⌃⌃** (double-tap Control): capture the display under the pointer (or all
  displays, see Settings) and solve. A new ⌃⌃ replaces a capture in progress.
- **⌘⌘** (double-tap Command): show or hide the answers. They are hidden when they
  arrive, unless "Keep visible" is on and answers were showing.
- Answers are drawn as plain text in each question's own colour and size, in the
  answer box, on the blank line or under the question; a multiple-choice answer is
  the option's letter in **yellow** at the end of the question.
- The menu-bar eye shows the state: plain (idle), dashed (working), blue dot
  (answers ready), red dot (a problem, explained at the top of the menu).
- **Hard mode** (the switch in the menu) answers with the current provider's most
  capable model (Claude Fable 5.2, Gemini 4 Carbon, or the newest full-size model
  of your ChatGPT account) until you turn it off; then Peek goes back to the model
  chosen in Settings. It is slower and costs several times more per capture, so it
  is never switched on automatically.
- **Diagrams.** When the screen shows a figure (a technical drawing, a circuit, a
  graph, a labelled illustration), Peek also sends an enlarged copy of it, so
  dimension lines and small labels are easier to read (about 1,600 input tokens
  per figure, two at most). Figures are found on your Mac; browser chrome, forms
  and photos are left out, and a figure that is already large on screen is not
  sent twice.

### Settings

- **Provider:** pick Claude, ChatGPT or Gemini, connect it, and choose a model
  (each provider remembers its own).
- **Hotkeys & capture:** double-tap speed (200-500 ms; regular shortcuts like ⌘C
  never trigger it), which display to capture, launch at login, and **Save
  questions** (off by default): after every capture, a picture of each question
  (its text, answer area and nearby figure, without answers) is saved as a PNG in
  a folder you choose.
- **Overlay:** answer text size ("Match the question" or a fixed size), keep
  answers visible across captures, and a preview.
- **Show work** (off by default): the model also writes out its working, shown in
  a box on screen together with the answers (⌘⌘ shows and hides both). Drag the
  box on the picture of your display to move it, drag an edge or corner to resize
  it; the text is sized to fit the box. Its colour is yours to pick (yellow by
  default). Asking for the working costs more output tokens per capture.

## Providers

**Claude (Anthropic API key).** Paste a key from console.anthropic.com into
**API key** and press Return. Models: Claude Fable 5.2 (most capable, slowest and
priciest), Opus 5.5, Sonnet 5.5 (default), Haiku 5.5 (fastest and cheapest).
**Test connection** checks the key and model without using credit. For Fable,
Opus and Sonnet, a request Claude declines on safety grounds is retried on
another Claude model by Anthropic (billed at that model's rates).

**ChatGPT (Sign in with ChatGPT, your Plus or Pro plan, no API key).** Click
**Continue with ChatGPT**; your browser opens OpenAI's sign-in, and after you
approve, Peek is connected ("Signed in as …", "Using ChatGPT plan"). Answers
count against your plan's usage limits (**Manage usage** opens
chatgpt.com/settings/usage). This uses OpenAI's preview flow for local and
open-source apps; if you decline plan usage while signing in, Peek stays signed
in but cannot answer until you choose **Allow plan usage**. **Sign out** revokes
Peek's access. If OpenAI could not confirm it, you can also disconnect Peek in
ChatGPT → Settings → Security.

**Gemini (Google AI Studio API key).** **Get a free key in Google AI Studio**
opens aistudio.google.com/apikey; paste the key (it starts with `AIza`). The model
list comes from your account (default Gemini 3.8 Flash). On the free tier, Google
may use your requests to improve its products, so avoid capturing private
information. Google does not allow apps to use a Google account or Gemini
subscription sign-in, so there is none.

## Privacy

- Screenshots are kept in memory and go only to the provider you pick; they are
  never written to disk, except the question pictures Save questions writes to
  the folder you chose, and with `--debug`, into
  `~/Library/Application Support/Peek/debug/`.
- API keys and ChatGPT tokens live only in your macOS Keychain. `settings.json`
  holds no secrets, and nothing secret is logged.
- Peek's own windows are kept out of its screenshots.

## Development setup

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh   # if you don't have uv
uv sync                                            # Python 3.13 + dependencies into .venv
uv run ruff check . && uv run ruff format --check .
```

Keep the checkout outside iCloud-synced folders (`~/Documents`, `~/Desktop`):
synced folders add extended attributes that break code signing, and they would
upload `.venv`, `build/` and `dist/`.

## Building and running

```bash
scripts/build.sh                     # alias build: dist/Peek.app runs your source tree live
scripts/build.sh release             # full standalone build (~115 MB)
open dist/Peek.app --args --debug    # --debug adds a Diagnostics submenu and verbose logs
```

- Always launch the `.app` with `open` (or Finder). macOS grants Screen Recording
  and Input Monitoring to the app bundle that asks; started from Terminal
  (`uv run python -m peek`), the request is attributed to Terminal instead.
- An alias build does not need rebuilding after source edits: quit Peek and open it again.
- Check that a release build contains every provider SDK (no network; works while
  Peek runs): `open -n -W dist/Peek.app --args --check-imports /tmp/peek-imports.txt`
- Intel build (on Apple Silicon, needs Rosetta):
  `PEEK_ARCH=x86_64 scripts/build.sh release` builds `dist-x86_64/Peek.app` with an
  x86_64 Python in `.venv-x86_64`. Only one Peek runs at a time: quit the other
  before opening it.
- Logs: `~/Library/Logs/Peek/peek.log` (secrets are redacted before anything is written).
- Offscreen renders of the custom-drawn UI, for checking against Figma:
  `uv run python -m peek.snapshot /tmp/peek-snapshots`.

## Code signing and permissions (one-time setup)

macOS remembers Screen Recording and Input Monitoring permissions per *code
signature*. py2app's default ad-hoc signature changes on every build, so every
rebuild would look like a new app and lose its permissions. Create a local
signing identity once:

```bash
scripts/make-dev-cert.sh
```

It creates a self-signed "Peek Dev" code-signing certificate in your login
keychain (macOS may ask for your login password; choose **Always Allow**).
`scripts/build.sh` uses it automatically from then on. Never delete or
regenerate it: a new certificate counts as a new signer and resets every permission.

- Screen Recording / Input Monitoring grants survive rebuilds, alias and release alike.
- Keychain items: after a *release* rebuild, macOS may ask once whether the new
  build may read them (choose Always Allow). Only an Apple-issued certificate
  avoids that.
- To start over with permissions: `scripts/reset-permissions.sh` (onboarding
  shows again when `onboarding_done` is false in
  `~/Library/Application Support/Peek/settings.json`).
- Input Monitoring: Peek creates its listen-only event tap only after the grant,
  and polls for it. If the menu says "Reopen Peek to turn on hotkeys", quit and
  reopen it. Peek never records which keys you press; it only logs completed
  double-taps. Hotkeys are ignored while macOS secure input is on (a password
  field is focused, or Terminal's Secure Keyboard Entry).
- The bundle identifier is `local.peek.Peek` (in `peek/__init__.py`). Changing it
  resets all permissions, Keychain items and the login item.
- Launch at login uses `SMAppService`; macOS may list Peek under System Settings
  → General → Login Items and ask you to allow it. It works best from
  /Applications.

## Project layout

```
peek/            the app
peek/providers/  catalog.py (what each provider is), claude.py, gemini.py,
                 chatgpt.py, oauth_chatgpt.py (Sign in with ChatGPT)
packaging/       py2app setup.py and main script (kept away from pyproject.toml on purpose)
scripts/         build.sh, make-dev-cert.sh, reset-permissions.sh, make-icon.py
```

## Known limitations

- macOS 15 and later periodically ask whether Peek may keep capturing the
  screen ("bypass the system private window picker"). There is no opt-out for
  this kind of app.
- Double-tapping Command also opens Type to Siri if that shortcut is set to
  "Press Command twice" (System Settings > Apple Intelligence & Siri). Peek can
  only listen to keys, not block them; change the Siri shortcut if they collide.
- Answers are pinned to screen positions: scrolling or moving windows misaligns
  them until the next capture. Changing displays (plugging one in, a new
  resolution) clears them.
- Answer size is matched to the question from the recognized text's width in the
  system font. Monospace pages are corrected; serif pages (Times) come out about
  6-14% smaller than the question text. A fixed size can be set in Settings.
- With no background behind them, answers can be hard to read on busy or dark
  content, and the yellow multiple-choice letter is low-contrast on white pages.
- Diagram detection looks for line drawings on a mostly plain background.
  Shaded or coloured illustrations are found when they have labels in them;
  pie charts and unlabelled colour art are usually missed (they look like
  photos). Some pictures (clip art, logos, an emoji grid, a blank table) can be
  sent as a "diagram", which only costs tokens. The enlarged copy adds pixels per
  stroke, not detail: a blurry figure stays blurry.
- ChatGPT: Sign in with ChatGPT is an OpenAI preview. Whether its route accepts a
  JSON schema for the answer is undocumented; if it refuses one, Peek asks again
  without it and parses the reply leniently. Only eligible plans can use it.
- Gemini and ChatGPT answer positions use 0-1000 coordinates (their convention);
  Claude uses the screenshot's pixels.
