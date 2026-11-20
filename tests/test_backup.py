"""avimsin-backup: sqlite3 komutu olmadan, WAL'daki yazmalar dahil tutarlı yedek."""

from __future__ import annotations

import sqlite3

import pytest

from avimsin.backup import backup_db, main
from avimsin.storage.db import connect, save_coin


def test_backup_contains_uncheckpointed_wal_writes(tmp_path):
    src, dst = tmp_path / "a.sqlite", tmp_path / "out" / "b.sqlite"
    conn = connect(src)  # bağlantı açık: son yazmalar -wal'da, ana dosyada değil
    with conn:
        save_coin(conn, "C" * 44, 7, chain="solana")
    assert (tmp_path / "a.sqlite-wal").stat().st_size > 0
    backup_db(src, dst)
    copy = sqlite3.connect(dst)
    assert copy.execute("SELECT address, first_block FROM coins").fetchall() == [("C" * 44, 7)]
    assert copy.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    conn.close()


def test_missing_db_is_an_error(tmp_path):
    with pytest.raises(SystemExit) as exc:
        main([str(tmp_path / "yok.sqlite"), str(tmp_path / "o.sqlite")])
    assert "yedek alınamadı" in str(exc.value)
