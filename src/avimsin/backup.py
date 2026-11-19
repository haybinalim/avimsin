"""avimsin-backup: SQLite DB'nin tutarlı (WAL dahil) yedeğini alır; sqlite3 komutu gerekmez.

Kullanım::

    avimsin-backup <db-yolu> <hedef-yolu>
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path


def backup_db(src: str | Path, dst: str | Path) -> None:
    """``sqlite3.Connection.backup`` ile kopyalar: açık yazma ve -wal içeriği tutarlı gelir."""
    if not Path(src).is_file():
        raise FileNotFoundError(f"DB yok: {src}")
    Path(dst).parent.mkdir(parents=True, exist_ok=True)
    source = sqlite3.connect(src, timeout=30)  # ro modu WAL'da -shm yoksa açılamaz; okuma yeterli
    target = sqlite3.connect(dst)
    try:
        source.backup(target)
    finally:
        target.close()
        source.close()


def main(argv: list[str] | None = None) -> None:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 2:
        raise SystemExit("kullanım: avimsin-backup <db-yolu> <hedef-yolu>")
    try:
        backup_db(args[0], args[1])
    except (OSError, sqlite3.Error) as exc:
        raise SystemExit(f"HATA: yedek alınamadı: {exc}") from exc
    print(f"yedek: {args[1]}")
