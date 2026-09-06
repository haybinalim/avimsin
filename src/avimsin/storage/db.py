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
    first_seen_block INTEGER NOT NULL,
    is_contract INTEGER,
    verdict TEXT,
    verdict_rule TEXT,
    verdict_reason TEXT
);
CREATE TABLE IF NOT EXISTS purchases (
    coin TEXT NOT NULL,
    wallet TEXT NOT NULL,
    block INTEGER NOT NULL,
    tx TEXT NOT NULL,
    PRIMARY KEY (coin, tx, wallet)
);
CREATE INDEX IF NOT EXISTS idx_purchases_coin_block ON purchases (coin, block);
CREATE TABLE IF NOT EXISTS scores (
    wallet TEXT PRIMARY KEY,
    winrate REAL NOT NULL,
    net_pnl INTEGER NOT NULL,
    trades INTEGER NOT NULL,
    frequency REAL NOT NULL,
    score REAL NOT NULL
);
"""

# Faz 2'den önce oluşturulan veritabanlarına verdict kolonlarını ekler.
MIGRATION = """
ALTER TABLE wallets ADD COLUMN is_contract INTEGER;
ALTER TABLE wallets ADD COLUMN verdict TEXT;
ALTER TABLE wallets ADD COLUMN verdict_rule TEXT;
ALTER TABLE wallets ADD COLUMN verdict_reason TEXT;
"""

VERDICT_OK = "ok"
VERDICT_BOT = "bot"
VERDICT_DUMP = "dump"


def connect(path: str | Path) -> sqlite3.Connection:
    """Veritabanını açar (gerekirse oluşturur), şemayı ve migration'ı uygular."""
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA)
    columns = {row[1] for row in conn.execute("PRAGMA table_info(wallets)")}
    if "verdict" not in columns:
        conn.executescript(MIGRATION)
    conn.commit()
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


def save_verdict(conn: sqlite3.Connection, wallet: str, verdict: str, rule: str, reason: str) -> None:
    """Filtre sonucunu cüzdan kaydına işler."""
    conn.execute(
        "UPDATE wallets SET verdict = ?, verdict_rule = ?, verdict_reason = ? WHERE address = ?",
        (verdict, rule, reason, wallet.lower()),
    )


TOKEN_UNIT = 10**18  # ERC-20 standart ondalığı; P&L ham birimden buna çevrilir


def save_score(
    conn: sqlite3.Connection,
    wallet: str,
    winrate: float,
    net_pnl: int,
    trades: int,
    frequency: float,
    score: float,
) -> None:
    """Skor tablosuna yazar veya günceller.

    ``net_pnl`` ham token biriminden (10^18) tam token birimine çevrilerek
    saklanır: milyar-arzlı token'ların tek transferi 10^27 ham birim taşıyabilir,
    SQLite INTEGER sınırı 9.2*10^18'dir. Sıralama ölçeği korunur.
    """
    pnl_in_tokens = net_pnl // TOKEN_UNIT
    conn.execute(
        "INSERT OR REPLACE INTO scores (wallet, winrate, net_pnl, trades, frequency, score)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (wallet.lower(), winrate, pnl_in_tokens, trades, frequency, score),
    )
