#!/bin/bash
# Build dist/Peek.app with py2app, then sign it.
#
#   scripts/build.sh            alias build (development; sources stay live, no rebuild on edits)
#   scripts/build.sh release    full standalone build
#   PEEK_ARCH=x86_64 scripts/build.sh release
#                               Intel build on Apple Silicon (Rosetta): its own x86_64
#                               Python and .venv-x86_64, output in dist-x86_64/
#
# Signing: if the "Peek Dev" code-signing identity exists (scripts/make-dev-cert.sh),
# the bundle is signed with it, so macOS keeps Screen Recording / Input Monitoring
# grants across rebuilds. Otherwise it is signed ad-hoc (py2app does not sign x86_64
# builds itself) and macOS treats every build as a new app.
set -euo pipefail

MODE="${1:-alias}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
HOST_ARCH="$(uname -m)"
ARCH="${PEEK_ARCH:-$HOST_ARCH}"
DIST="$ROOT/dist"
UV_PY=()
if [[ "$ARCH" != "$HOST_ARCH" ]]; then
  # py2app builds for the interpreter's own architecture: use a second Python.
  export UV_PROJECT_ENVIRONMENT="$ROOT/.venv-$ARCH"
  UV_PY=(--python "cpython-3.13-macos-$ARCH-none")
  DIST="$ROOT/dist-$ARCH"
fi
export PEEK_ARCH="$ARCH"
APP="$DIST/Peek.app"
CERT_NAME="${PEEK_SIGN_IDENTITY:-Peek Dev}"

case "$MODE" in
  alias) PY2APP_FLAGS=(-A) ;;
  release) PY2APP_FLAGS=() ;;
  *) echo "usage: $0 [alias|release]" >&2; exit 2 ;;
esac

cd "$ROOT"
BUNDLE_ID="$(uv run ${UV_PY[@]+"${UV_PY[@]}"} --quiet python -c 'from peek import BUNDLE_ID; print(BUNDLE_ID)')"

echo "==> py2app ($MODE)"
rm -rf "$ROOT/build" "$APP"
mkdir -p "$ROOT/build"
BUILD_LOG="$ROOT/build/py2app.log"
(
  # Run from packaging/ so setuptools does not read the project's pyproject.toml.
  cd "$ROOT/packaging"
  # (bash 3.2 + set -u: an empty array needs the ${a[@]+...} form)
  uv run ${UV_PY[@]+"${UV_PY[@]}"} --project "$ROOT" python setup.py py2app \
    ${PY2APP_FLAGS[@]+"${PY2APP_FLAGS[@]}"} --dist-dir "$DIST" --bdist-base "$ROOT/build"
) > "$BUILD_LOG" 2>&1 || { tail -40 "$BUILD_LOG"; echo "py2app failed; full log: $BUILD_LOG" >&2; exit 1; }
tail -3 "$BUILD_LOG"

# Extended attributes (Finder info, quarantine, iCloud) make codesign fail.
# -s: do not follow symlinks. Alias bundles contain dangling links (site.pyc,
# config) and links into .venv, which must not be touched.
xattr -crs "$APP"

# The identity is looked up by name but used by SHA-1 hash, so a duplicate name
# can never make codesign ambiguous. Untrusted self-signed identities are fine.
IDENTITY="$(security find-identity -p codesigning 2>/dev/null \
  | awk -v name="\"$CERT_NAME\"" 'index($0, name) { print $2; exit }')"

sign() { codesign --force --timestamp=none --sign "$IDENTITY" "$@"; }

if [[ -z "$IDENTITY" ]]; then
  IDENTITY="-"
  echo "==> No \"$CERT_NAME\" signing identity: signing ad-hoc."
  echo "    Permissions will reset on every rebuild. Run scripts/make-dev-cert.sh once to fix that."
else
  echo "==> Signing with \"$CERT_NAME\" ($IDENTITY)"
fi

if [[ "$MODE" == "release" ]]; then
  # Inside out: every nested Mach-O (deepest first), then frameworks, then the app.
  # No --deep: it signs in the wrong order and hides failures. find -type f skips
  # symlinks; extension modules inside zip archives are not loadable and need no signature.
  MAIN_EXE="$APP/Contents/MacOS/Peek"
  while IFS= read -r f; do
    [[ "$f" == "$MAIN_EXE" ]] && continue
    if file -b "$f" | grep -q "Mach-O"; then sign "$f" >/dev/null; fi
  done < <(find "$APP/Contents" -type f -not -path "*/_CodeSignature/*" \
             | awk '{ print gsub("/", "/") "\t" $0 }' | sort -rn | cut -f2-)
  while IFS= read -r fw; do sign "$fw" >/dev/null; done \
    < <(find "$APP/Contents" -type d -name "*.framework" | sort -r)
fi
# Alias builds: only the stub executable is a real Mach-O; Contents/MacOS/python
# is a symlink into .venv and is intentionally left outside the signature.
sign --identifier "$BUNDLE_ID" "$APP"

echo "==> Verify"
if [[ "$MODE" == "release" ]]; then
  codesign --verify --deep --strict "$APP"
else
  # --strict rejects the alias bundle's symlink into .venv; plain verify is expected to pass.
  codesign --verify "$APP"
fi
codesign -d -r- "$APP" 2>&1 | grep designated
echo "==> Built $APP"
echo "    Launch it with: open \"$APP\" --args --debug"
