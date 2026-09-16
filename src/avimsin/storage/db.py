"""SQLite kalıcılık — coin'ler, cüzdanlar ve alımlar.

EVM adresleri canonical (küçük harf) saklanır; base58 (Solana) adresleri
büyük/küçük harf duyarlı haliyle saklanır — bkz. normalize_address.
`coins.chain` coin'in hangi ağdan tarandığını tutar (dashboard filtresi);
eski kayıtlarda NULL kalır ve `save_coin` zinciri verildiğinde tamamlanır.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS coins (
    address TEXT PRIMARY KEY,
    first_block INTEGER NOT NULL,
    chain TEXT
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


def normalize_address(address: str) -> str:
    """Adresi saklama biçimine çevirir.

    EVM adresleri (0x önekli) büyük/küçük harf duyarsız olduğu için küçültülür;
    base58 (Solana) adresleri 0x taşıyamaz ve harf duyarlıdır — olduğu gibi
    saklanır. Zincir bilgisine gerek bırakmaz: önek yeterli ayırt edicidir.
    Önek kontrolü harf duyarsızdır ("0X" de EVM'dir) ve bu güvenlidir: base58
    alfabesinde '0' (sıfır) yoktur, yani base58 adres asla 0 ile başlamaz.
    """
    return address.lower() if address[:2].lower() == "0x" else address


def connect(path: str | Path) -> sqlite3.Connection:
    """Veritabanını açar (gerekirse oluşturur), şemayı ve migration'ı uygular."""
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA)
    columns = {row[1] for row in conn.execute("PRAGMA table_info(wallets)")}
    if "verdict" not in columns:
        conn.executescript(MIGRATION)
    # Solana desteğinden önceki veritabanları: chain kolonu sonradan eklenir.
    coin_columns = {row[1] for row in conn.execute("PRAGMA table_info(coins)")}
    if "chain" not in coin_columns:
        conn.execute("ALTER TABLE coins ADD COLUMN chain TEXT")
    conn.commit()
    return conn


def save_coin(
    conn: sqlite3.Connection, address: str, first_block: int, chain: str | None = None
) -> None:
    """Coin'i kaydeder; zincir verilmişse (ve kayıtta yoksa) tamamlar.

    Mevcut kaydın `first_block`'u korunur — yeniden tarama ilk bloğu bozmasın.
    """
    conn.execute(
        "INSERT INTO coins (address, first_block, chain) VALUES (?, ?, ?)"
        " ON CONFLICT(address) DO UPDATE SET chain = COALESCE(coins.chain, excluded.chain)",
        (normalize_address(address), first_block, chain),
    )


def set_chain(conn: sqlite3.Connection, address: str, chain: str) -> None:
    """Zinciri yalnızca kayıtta yoksa yazar (legacy kayıtların tamamlanması)."""
    conn.execute(
        "UPDATE coins SET chain = ? WHERE address = ? AND chain IS NULL",
        (chain, normalize_address(address)),
    )


def coin_chains(conn: sqlite3.Connection) -> dict[str, str]:
    """address → chain eşlemesi (zinciri bilinmeyenler dahil edilmez)."""
    return {
        row[0]: row[1]
        for row in conn.execute("SELECT address, chain FROM coins WHERE chain IS NOT NULL")
    }


def wallet_chains(conn: sqlite3.Connection) -> dict[str, str]:
    """wallet → chain eşlemesi: cüzdanın alım yaptığı ilk coin'in zinciri.

    Bir cüzdan birden çok zincirde görünebilir (aynı adres farklı ağlarda);
    en erken bloklu kayıt kazanır ki dashboard tek satırda göstersin.
    """
    rows = conn.execute(
        """
        SELECT p.wallet, c.chain
        FROM purchases p JOIN coins c ON c.address = p.coin
        WHERE c.chain IS NOT NULL
        ORDER BY p.block
        """
    ).fetchall()
    out: dict[str, str] = {}
    for wallet, chain in rows:
        out.setdefault(wallet, chain)
    return out


def save_purchase(conn: sqlite3.Connection, coin: str, wallet: str, block: int, tx: str) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO purchases (coin, wallet, block, tx) VALUES (?, ?, ?, ?)",
        (normalize_address(coin), normalize_address(wallet), block, tx),
    )
    conn.execute(
        "INSERT OR IGNORE INTO wallets (address, first_seen_block) VALUES (?, ?)",
        (normalize_address(wallet), block),
    )


def save_verdict(conn: sqlite3.Connection, wallet: str, verdict: str, rule: str, reason: str) -> None:
    """Filtre sonucunu cüzdan kaydına işler."""
    conn.execute(
        "UPDATE wallets SET verdict = ?, verdict_rule = ?, verdict_reason = ? WHERE address = ?",
        (verdict, rule, reason, normalize_address(wallet)),
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
    token_unit: int = TOKEN_UNIT,
) -> None:
    """Skor tablosuna yazar veya günceller.

    ``net_pnl`` ham token biriminden tam token birimine ``token_unit`` ile
    çevrilerek saklanır: milyar-arzlı token'ların tek transferi 10^27 ham birim
    taşıyabilir, SQLite INTEGER sınırı 9.2*10^18'dir. Sıralama ölçeği korunur.
    ``token_unit`` = 10**decimals; EVM'de 18, Solana'da mint'ten okunur.
    """
    pnl_in_tokens = net_pnl // token_unit
    conn.execute(
        "INSERT OR REPLACE INTO scores (wallet, winrate, net_pnl, trades, frequency, score)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (normalize_address(wallet), winrate, pnl_in_tokens, trades, frequency, score),
    )
