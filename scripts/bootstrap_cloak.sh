#!/usr/bin/env bash
# One-command deployment for Hermes Unlocked — Cloak Edition.
#
# This intentionally installs no local Playwright browser. The configured
# `cloak` provider connects to the Manager-owned CloakBrowser profile over CDP.
#
# Example:
#   curl -fsSL https://raw.githubusercontent.com/Iwakishirokoshu/Hermes-Unlocked-Cloak-Edition-0.19/main/scripts/bootstrap_cloak.sh | sudo bash

set -euo pipefail

REPOSITORY_RAW="https://raw.githubusercontent.com/Iwakishirokoshu/Hermes-Unlocked-Cloak-Edition-0.19"
BRANCH="${HERMES_CLOAK_BRANCH:-main}"
HERMES_CODE_DIR="${HERMES_INSTALL_DIR:-/usr/local/lib/hermes-agent}"
HERMES_DATA_DIR="${HERMES_HOME:-/root/.hermes}"

info() { printf '[cloak-bootstrap] %s\n' "$*"; }
fail() { printf '[cloak-bootstrap] ERROR: %s\n' "$*" >&2; exit 1; }

usage() {
  cat <<'EOF'
Usage: sudo bash bootstrap_cloak.sh [options]

Options:
  --branch NAME        Git branch to install (default: main)
  --dir PATH           Hermes checkout directory
  --hermes-home PATH   Hermes data/config directory
  -h, --help           Show this help

The bootstrap installs Hermes non-interactively, skips the local Playwright
Chromium download, provisions the protected Cloak Manager/CDP path, and sets
browser.cloud_provider=cloak. It never accepts or prints model/API secrets.
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --branch)
      [[ $# -ge 2 ]] || fail "--branch requires a value"
      BRANCH="$2"; shift 2 ;;
    --dir)
      [[ $# -ge 2 ]] || fail "--dir requires a value"
      HERMES_CODE_DIR="$2"; shift 2 ;;
    --hermes-home)
      [[ $# -ge 2 ]] || fail "--hermes-home requires a value"
      HERMES_DATA_DIR="$2"; shift 2 ;;
    -h|--help)
      usage; exit 0 ;;
    *)
      fail "unknown option: $1" ;;
  esac
done

[[ "${EUID:-$(id -u)}" -eq 0 ]] || fail "Run as root: curl ... | sudo bash"
[[ "$BRANCH" =~ ^[A-Za-z0-9._/-]+$ && "$BRANCH" != *..* && "$BRANCH" != -* ]] \
  || fail "unsafe branch name"

BOOTSTRAP_TMPDIR="$(mktemp -d "${TMPDIR:-/tmp}/hermes-cloak-bootstrap.XXXXXX")"
trap 'rm -rf "$BOOTSTRAP_TMPDIR"' EXIT
umask 077

download() {
  local url="$1" destination="$2"
  if command -v curl >/dev/null 2>&1; then
    curl -fsSL --retry 3 --connect-timeout 15 "$url" -o "$destination"
  elif command -v wget >/dev/null 2>&1; then
    wget -q --tries=3 --timeout=15 -O "$destination" "$url"
  else
    fail "curl or wget is required to fetch the installer"
  fi
}

CORE_INSTALLER="$BOOTSTRAP_TMPDIR/install.sh"
info "Downloading the installer entrypoint for branch '$BRANCH'..."
download "$REPOSITORY_RAW/$BRANCH/scripts/install.sh" "$CORE_INSTALLER"
chmod 0700 "$CORE_INSTALLER"

info "Installing Hermes and its Node driver (local Chromium is intentionally skipped)..."
HERMES_HOME="$HERMES_DATA_DIR" HERMES_INSTALL_DIR="$HERMES_CODE_DIR" \
  bash "$CORE_INSTALLER" \
    --non-interactive --skip-setup --skip-browser \
    --branch "$BRANCH" --dir "$HERMES_CODE_DIR" --hermes-home "$HERMES_DATA_DIR"

export PATH="$HERMES_DATA_DIR/bin:$HERMES_DATA_DIR/node/bin:$PATH"

CLOAK_INSTALLER="$HERMES_CODE_DIR/scripts/install_cloak.sh"
[[ -f "$CLOAK_INSTALLER" ]] || fail "Cloak installer was not found in $HERMES_CODE_DIR"

info "Provisioning CloakBrowser-Manager, protected CDP proxy, and provider config..."
HERMES_HOME="$HERMES_DATA_DIR" HERMES_INSTALL_DIR="$HERMES_CODE_DIR" \
  bash "$CLOAK_INSTALLER" --configure-provider --strict

info "Ready. Add your already-issued model credential only to $HERMES_DATA_DIR/.env before starting Hermes."
