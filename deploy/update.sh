#!/usr/bin/env bash
# Avımsın güncelleme — kodu tazeler, venv'i senkronlar, servisi restart eder.
# Kullanım: sudo ./deploy/update.sh [--app-dir /opt/avimsin] [--rev main]
set -euo pipefail

APP_DIR="/opt/avimsin"
REV="main"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --app-dir) APP_DIR="$2"; shift 2 ;;
    --rev) REV="$2"; shift 2 ;;
    *) echo "bilinmeyen bayrak: $1" >&2; exit 2 ;;
  esac
done

if [[ $EUID -ne 0 ]]; then
  echo "root olarak çalıştırın: sudo ./deploy/update.sh" >&2; exit 2
fi

# APP_DIR bir git çalışma kopyasıysa çek; dosya kopyasıysa atla.
if [[ -d "$APP_DIR/.git" ]]; then
  git -C "$APP_DIR" fetch -q origin "$REV"
  git -C "$APP_DIR" checkout -q "$REV"
  git -C "$APP_DIR" pull -q --ff-only origin "$REV"
  echo "kod güncellendi: $(git -C "$APP_DIR" rev-parse --short HEAD)"
else
  echo "NOT: $APP_DIR git kopyası değil; kaynak manuel kopyalanmalı (install.sh dosya kopyası yapmıştı)."
fi

cd "$APP_DIR"
uv sync --frozen --no-dev 2>/dev/null || uv sync --no-dev
systemctl restart avimsin-watch.service
sleep 2
systemctl is-active --quiet avimsin-watch.service && echo "servis AYAKTA" || {
  echo "restart sonrası servis kalkmadı; log: journalctl -u avimsin-watch -e" >&2; exit 1;
}
