#!/bin/bash
# One-time setup: create a self-signed "Peek Dev" code-signing identity in your
# login keychain, so macOS keeps Peek's Screen Recording and Input Monitoring
# permissions across rebuilds.
#
# Run it yourself in Terminal (it may ask for your login keychain password):
#   scripts/make-dev-cert.sh
#
# - Idempotent: does nothing if a "Peek Dev" identity already exists.
# - Valid for 20 years. Never delete or regenerate it: a new certificate is a new
#   signer, and every permission you granted to Peek would be lost.
# - Not trusted system-wide and not added to any trust settings; code signing
#   does not need that.
set -euo pipefail

NAME="Peek Dev"
KEYCHAIN="$HOME/Library/Keychains/login.keychain-db"

if security find-identity -p codesigning 2>/dev/null | grep -q "\"$NAME\""; then
  echo "A \"$NAME\" code-signing identity already exists; nothing to do."
  security find-identity -p codesigning | grep "\"$NAME\""
  exit 0
fi

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
chmod 700 "$WORK"

cat > "$WORK/cert.cnf" <<EOF
[ req ]
distinguished_name = dn
x509_extensions = ext
prompt = no
[ dn ]
CN = $NAME
[ ext ]
basicConstraints = critical, CA:false
keyUsage = critical, digitalSignature
extendedKeyUsage = critical, codeSigning
subjectKeyIdentifier = hash
EOF

echo "==> Creating certificate and key"
/usr/bin/openssl req -x509 -newkey rsa:2048 -nodes -days 7300 \
  -keyout "$WORK/key.pem" -out "$WORK/cert.pem" -config "$WORK/cert.cnf" 2>/dev/null

# The .p12 password only protects the file for the few seconds it exists.
P12_PASS="$(/usr/bin/openssl rand -hex 16)"
/usr/bin/openssl pkcs12 -export -name "$NAME" -inkey "$WORK/key.pem" -in "$WORK/cert.pem" \
  -out "$WORK/peek-dev.p12" -passout "pass:$P12_PASS"

echo "==> Importing into your login keychain (codesign is allowed to use the key)"
security import "$WORK/peek-dev.p12" -k "$KEYCHAIN" -P "$P12_PASS" -T /usr/bin/codesign

echo "==> Test-signing a scratch binary"
echo "    If macOS asks to let codesign use the \"$NAME\" key, enter your login"
echo "    password and choose Always Allow, so later builds never stop to ask."
cp /usr/bin/true "$WORK/signing-test"
codesign --force --timestamp=none --sign "$NAME" "$WORK/signing-test"
codesign -d -r- "$WORK/signing-test" 2>&1 | grep designated

echo
security find-identity -p codesigning | grep "\"$NAME\""
echo "Done. Rebuild with scripts/build.sh; it signs with \"$NAME\" automatically."
