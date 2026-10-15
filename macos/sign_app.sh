#!/usr/bin/env bash
# Sign a JUA254 Display app bundle: sign_app.sh "/path/JUA254 Display.app"
# Uses a stable local identity, so macOS sees every rebuild as the same app and keeps its Screen
# Recording permission. Created once in the login keychain; ad-hoc signing is the fallback.
set -uo pipefail
app="$1"

# CI/release builds: sign with an identity already in a keychain (see .github/workflows/macos.yml).
if [[ -n "${JUA254_SIGN_IDENTITY:-}" ]]; then
  if codesign --force --sign "$JUA254_SIGN_IDENTITY" "$app"; then
    echo "Signed as \"$JUA254_SIGN_IDENTITY\"."
  else
    echo "Signing with \"$JUA254_SIGN_IDENTITY\" failed; using a throwaway signature instead."
    codesign --force --sign - "$app"
  fi
  exit 0
fi
identity="JUA254 Local Signing"
bundle_id="io.github.josh-eg112.jua254-display"
keychain="$HOME/Library/Keychains/login.keychain-db"

create_identity() {
  local tmp; tmp="$(mktemp -d)"
  cat > "$tmp/cert.cnf" <<CNF
[req]
distinguished_name = dn
x509_extensions = ext
prompt = no
[dn]
CN = $identity
[ext]
basicConstraints = critical, CA:false
keyUsage = critical, digitalSignature
extendedKeyUsage = critical, codeSigning
CNF
  echo "Creating the local code-signing certificate \"$identity\" (once)."
  echo "macOS may ask for your login password to trust it."
  /usr/bin/openssl req -x509 -newkey rsa:2048 -nodes -days 3650 -config "$tmp/cert.cnf" \
      -keyout "$tmp/key.pem" -out "$tmp/cert.pem" 2>/dev/null &&
    /usr/bin/openssl pkcs12 -export -inkey "$tmp/key.pem" -in "$tmp/cert.pem" -name "$identity" \
      -passout pass:jua254 -out "$tmp/identity.p12" &&
    security import "$tmp/identity.p12" -k "$keychain" -P jua254 -T /usr/bin/codesign >/dev/null &&
    security add-trusted-cert -r trustRoot -p codeSign -k "$keychain" "$tmp/cert.pem"
  local status=$?
  rm -rf "$tmp"
  return $status
}

signed=adhoc
if security find-certificate -c "$identity" "$keychain" >/dev/null 2>&1 || create_identity; then
  if codesign --force --sign "$identity" "$app" 2>/dev/null; then
    signed=stable
  else
    echo "Signing with \"$identity\" failed; using a throwaway signature instead."
  fi
else
  echo "Could not create the signing certificate; using a throwaway signature instead."
fi
[[ $signed == stable ]] || codesign --force --sign - "$app"

# Permission granted to an earlier differently-signed build would just be refused, so clear it
# whenever the signature kind changes; macOS then asks once more.
marker="$HOME/Library/Application Support/JUA254/signing"
mkdir -p "$(dirname "$marker")"
if [[ "$(cat "$marker" 2>/dev/null)" != "$signed" ]]; then
  tccutil reset ScreenCapture "$bundle_id" >/dev/null 2>&1 || true
  echo "$signed" > "$marker"
  echo "macOS will ask once for Screen Recording permission for JUA254 Display."
fi
[[ $signed == stable ]] && echo "Signed as \"$identity\": permission will persist across rebuilds." ||
  echo "Note: with a throwaway signature, each rebuild needs: tccutil reset ScreenCapture $bundle_id"
