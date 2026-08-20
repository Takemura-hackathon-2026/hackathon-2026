#!/usr/bin/env bash
set -euo pipefail

SOURCE_ROOT="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
INSTALL_ROOT="/opt/hackathon-2026/pi-client"
RGB_LIB_DISTRIBUTION="${RGB_LIB_DISTRIBUTION:-$HOME/rpi-rgb-led-matrix}"
TARGET_ID="${1:-}"

if [[ ! "$TARGET_ID" =~ ^[0-3]$ ]]; then
  printf '使い方: %s TARGET_ID(0..3)\n' "$0" >&2
  exit 2
fi

if [[ ! -f "$RGB_LIB_DISTRIBUTION/include/led-matrix.h" ]]; then
  printf 'error: rpi-rgb-led-matrix が見つかりません: %s\n' "$RGB_LIB_DISTRIBUTION" >&2
  printf '       RGB_LIB_DISTRIBUTION を指定するか、先にライブラリを配置してください。\n' >&2
  exit 1
fi

make -C "$SOURCE_ROOT" RGB_LIB_DISTRIBUTION="$RGB_LIB_DISTRIBUTION"

as_root() {
  if [[ "$(id -u)" == 0 ]]; then
    "$@"
  else
    sudo -n "$@"
  fi
}

as_root install -d -m 0755 "$INSTALL_ROOT"
as_root install -m 0755 "$SOURCE_ROOT/pi_client" "$INSTALL_ROOT/pi_client"
as_root install -m 0644 "$SOURCE_ROOT/pi-client@.service" /etc/systemd/system/pi-client@.service
as_root systemctl daemon-reload
as_root systemctl enable --now "pi-client@${TARGET_ID}.service"
as_root systemctl --no-pager --full status "pi-client@${TARGET_ID}.service"
