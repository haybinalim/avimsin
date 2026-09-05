"""SQLite kalıcılık — coin'ler, cüzdanlar ve alımlar.

Tüm adresler canonical (küçük harf) saklanır.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS coins (
    address TEXT PRIMARY KEY,
    first_block INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS wallets (
    address TEXT PRIMARY KEY,
    first_seen_block INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS purchases (
    coin TEXT NOT NULL,
    wallet TEXT NOT NULL,
    block INTEGER NOT NULL,
    tx TEXT NOT NULL,
    PRIMARY KEY (coin, tx, wallet)
);
CREATE INDEX IF NOT EXISTS idx_purchases_coin_block ON purchases (coin, block);
"""


def connect(path: str | Path) -> sqlite3.Connection:
    """Veritabanını açar (gerekirse oluşturur), şemayı uygular."""
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA)
    return conn


def save_coin(conn: sqlite3.Connection, address: str, first_block: int) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO coins (address, first_block) VALUES (?, ?)",
        (address.lower(), first_block),
    )


def save_purchase(conn: sqlite3.Connection, coin: str, wallet: str, block: int, tx: str) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO purchases (coin, wallet, block, tx) VALUES (?, ?, ?, ?)",
        (coin.lower(), wallet.lower(), block, tx),
    )
    conn.execute(
        "INSERT OR IGNORE INTO wallets (address, first_seen_block) VALUES (?, ?)",
        (wallet.lower(), block),
    )
