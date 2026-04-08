#!/usr/bin/env bash
# Fast incremental dev build for T3W1 firmware.
# Reuses the existing trezor-firmware-env.nix__main snapshot (no re-clone, no uv sync, no cargo fetch).
# Uses SCons incremental build (no make clean) — only recompiles changed files.
set -e -o pipefail

SNAPSHOT="trezor-firmware-env.nix__main"
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
NIX="/nix/var/nix/profiles/default/bin/nix-shell"
PRODUCTION="${PRODUCTION:-0}"
BITCOIN_ONLY="${BITCOIN_ONLY:-0}"
TREZOR_MODEL="${TREZOR_MODEL:-T3W1}"
BOOTLOADER_DEVEL="${BOOTLOADER_DEVEL:-0}"

DIRSUFFIX="-${TREZOR_MODEL}"
if [ "$BITCOIN_ONLY" = "1" ]; then DIRSUFFIX="${DIRSUFFIX}-bitcoinonly"; fi

mkdir -p "$DIR/build/core${DIRSUFFIX}/firmware"

echo ">>> Updating repo in snapshot to HEAD ($(git rev-parse --short HEAD))"
docker run \
  --name dev-build-update \
  -v "$DIR:/local:ro" \
  "$SNAPSHOT" \
  "$NIX" --run "
    cd /reproducible-build/trezor-firmware
    git remote add local /local 2>/dev/null || git remote set-url local /local
    git fetch local
    git reset --hard local/main
    git submodule update --init --recursive
    uv sync --locked
  " || (docker rm dev-build-update 2>/dev/null; exit 1)

docker commit dev-build-update "$SNAPSHOT"
docker rm dev-build-update

echo ">>> Incremental build (no clean) TREZOR_MODEL=$TREZOR_MODEL BITCOIN_ONLY=$BITCOIN_ONLY PRODUCTION=$PRODUCTION BOOTLOADER_DEVEL=$BOOTLOADER_DEVEL"
docker run --rm \
  -v "$DIR/build/core${DIRSUFFIX}":/build:z \
  --env BITCOIN_ONLY="$BITCOIN_ONLY" \
  --env TREZOR_MODEL="$TREZOR_MODEL" \
  --env PRODUCTION="$PRODUCTION" \
  --env BOOTLOADER_DEVEL="$BOOTLOADER_DEVEL" \
  "$SNAPSHOT" \
  "$NIX" --run "
    set -e
    cd /reproducible-build/trezor-firmware/core
    uv run make vendor build_firmware QUIET_MODE=1 BOOTLOADER_DEVEL=\$BOOTLOADER_DEVEL
    mkdir -p /build/firmware
    cp -v build/firmware/firmware.bin /build/firmware/firmware.bin
    echo '--- Fingerprint ---'
    sha256sum build/firmware/firmware.bin
  "

echo ">>> Done: build/core${DIRSUFFIX}/firmware/firmware.bin"
