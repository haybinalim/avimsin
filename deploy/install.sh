#!/usr/bin/env bash
# Avımsın VPS kurulumu — Oracle Free Tier (Ubuntu 22.04) varsayımıyla.
# Kullanım: sudo ./deploy/install.sh [--app-dir /opt/avimsin] [--chain robinhood] [--user ubuntu]
# İdempotent: tekrar çalıştırılabilir; mevcut .env ve DB'ye dokunmaz.
set -euo pipefail

APP_DIR="/opt/avimsin"
CHAIN="robinhood"
SVC_USER="$(logname 2>/dev/null || echo ubuntu)"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --app-dir) APP_DIR="$2"; shift 2 ;;
    --chain) CHAIN="$2"; shift 2 ;;
    --user) SVC_USER="$2"; shift 2 ;;
    *) echo "bilinmeyen bayrak: $1" >&2; exit 2 ;;
  esac
done

if [[ "$CHAIN" != "robinhood" && "$CHAIN" != "solana" ]]; then
  echo "chain robinhood|solana olmalı: $CHAIN" >&2; exit 2
fi
if [[ $EUID -ne 0 ]]; then
  echo "root olarak çalıştırın: sudo ./deploy/install.sh" >&2; exit 2
fi
id "$SVC_USER" >/dev/null 2>&1 || { echo "kullanıcı yok: $SVC_USER" >&2; exit 2; }

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(dirname "$SCRIPT_DIR")"

echo "== sistem paketleri =="
apt-get update -qq
apt-get install -y -qq python3.12 python3.12-venv curl ca-certificates sqlite3 >/dev/null

echo "== uygulama dizini ($APP_DIR) =="
mkdir -p "$APP_DIR"
# Kaynak: bu repo çalışma kopyası (git varsa o, yoksa dosya kopyası).
if [[ -d "$REPO_DIR/.git" ]] && command -v git >/dev/null; then
  git -C "$REPO_DIR" rev-parse HEAD >/dev/null
  rm -rf "$APP_DIR/src" "$APP_DIR/dashboard" "$APP_DIR/deploy"
  cp -r "$REPO_DIR/src" "$REPO_DIR/dashboard" "$REPO_DIR/deploy" "$REPO_DIR/pyproject.toml" "$REPO_DIR/uv.lock" "$REPO_DIR/README.md" "$APP_DIR/"
else
  cp -r "$REPO_DIR/src" "$REPO_DIR/dashboard" "$REPO_DIR/deploy" "$REPO_DIR/pyproject.toml" "$REPO_DIR/README.md" "$APP_DIR/"
fi
mkdir -p "$APP_DIR/data"
touch "$APP_DIR/data/.gitkeep" 2>/dev/null || true

echo "== uv + sanal ortam =="
if ! command -v uv >/dev/null; then
  curl -LsSf https://astral.sh/uv/install.sh | env UV_INSTALL_DIR=/usr/local/bin sh >/dev/null
fi
cd "$APP_DIR"
uv sync --frozen --no-dev 2>/dev/null || uv sync --no-dev

echo "== .env =="
if [[ ! -f "$APP_DIR/.env" ]]; then
  cp "$APP_DIR/deploy/avimsin.env.example" "$APP_DIR/.env"
  chmod 600 "$APP_DIR/.env"
  echo "UYARI: $APP_DIR/.env doldurulmalı (TELEGRAM_*, RPC_*). Servis yine de kurulur."
else
  echo "mevcut .env korunuyor."
fi

echo "== sahiplik =="
chown -R "$SVC_USER:$SVC_USER" "$APP_DIR"

echo "== systemd servisi =="
UNIT_SRC="$APP_DIR/deploy/avimsin-watch.service.template"
UNIT_DST="/etc/systemd/system/avimsin-watch.service"
sed -e "s|APP_DIR|$APP_DIR|g" -e "s|CHAIN_NAME|$CHAIN|g" "$UNIT_SRC" > "$UNIT_DST"
# Servis dos kullanıcı adına çalışır (root yetkisi gerekmez).
if ! grep -q "^User=" "$UNIT_DST"; then
  sed -i "/^\[Service\]/a User=$SVC_USER" "$UNIT_DST"
fi
systemd-analyze verify "$UNIT_DST"
systemctl daemon-reload
systemctl enable --now avimsin-watch.service

echo "== duman testi =="
sleep 2
systemctl is-active --quiet avimsin-watch.service && echo "servis AYAKTA" || {
  echo "servis ayağa kalkamadı; log: journalctl -u avimsin-watch -e" >&2; exit 1;
}
echo "kurulum tamam: journalctl -u avimsin-watch -f ile izleyin."
