#!/usr/bin/env bash
# Avımsın DB yedeği — data/avimsin.sqlite'ı tarihli kopyalar.
# Kullanım: ./deploy/backup.sh [--app-dir /opt/avimsin] [--out-dir ~/avimsin-backup]
# Root gerekmez; APP_DIR okunabilsin yeter.
set -euo pipefail

APP_DIR="/opt/avimsin"
OUT_DIR="$HOME/avimsin-backup"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --app-dir) APP_DIR="$2"; shift 2 ;;
    --out-dir) OUT_DIR="$2"; shift 2 ;;
    *) echo "bilinmeyen bayrak: $1" >&2; exit 2 ;;
  esac
done

DB="$APP_DIR/data/avimsin.sqlite"
[[ -f "$DB" ]] || { echo "DB yok: $DB" >&2; exit 1; }

mkdir -p "$OUT_DIR"
STAMP="$(date +%Y%m%d-%H%M%S)"
cp "$DB" "$OUT_DIR/avimsin-$STAMP.sqlite"
echo "yedek: $OUT_DIR/avimsin-$STAMP.sqlite"
# Son 14 yedeği tut, eskileri sil.
ls -t "$OUT_DIR"/avimsin-*.sqlite 2>/dev/null | tail -n +15 | xargs -r rm -f
