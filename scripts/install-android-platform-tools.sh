#!/bin/sh
# Install Google platform-tools (modern adb with `adb pair`).
# Debian/Ubuntu `adb` packages are still ~29.x and reject `pair`.
#
# Official Linux zip is x86_64 only — build/run phone + scrcpy images as
# linux/amd64 (Compose `platform:` / Dockerfile FROM --platform).
set -eu

ANDROID_HOME="${ANDROID_HOME:-/opt/android-sdk}"
PT_ZIP_URL="${PT_ZIP_URL:-https://dl.google.com/android/repository/platform-tools-latest-linux.zip}"

apt-get update
apt-get install -y --no-install-recommends ca-certificates curl unzip

curl -fsSL "${PT_ZIP_URL}" -o /tmp/platform-tools.zip
mkdir -p "${ANDROID_HOME}"
unzip -q /tmp/platform-tools.zip -d "${ANDROID_HOME}"
rm -f /tmp/platform-tools.zip

apt-get purge -y --auto-remove curl unzip
rm -rf /var/lib/apt/lists/*

ln -sf "${ANDROID_HOME}/platform-tools/adb" /usr/local/bin/adb
adb version
# Fail the image build if pair is missing (old zip / wrong artifact).
adb help 2>&1 | grep -Eq '(^| )pair( |$)' || {
  echo "error: installed adb lacks 'pair' — check platform-tools zip" >&2
  exit 1
}
