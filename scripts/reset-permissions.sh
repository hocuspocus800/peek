#!/bin/bash
# Forget Peek's Screen Recording and Input Monitoring grants (for testing
# onboarding, or after a signing change left stale entries in System Settings).
# Keychain items (API keys) are not touched.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BUNDLE_ID="$(cd "$ROOT" && uv run --quiet python -c 'from peek import BUNDLE_ID; print(BUNDLE_ID)')"
for service in ScreenCapture ListenEvent; do
  tccutil reset "$service" "$BUNDLE_ID" || true
done
echo "Reset Screen Recording and Input Monitoring for $BUNDLE_ID. Quit and reopen Peek."
