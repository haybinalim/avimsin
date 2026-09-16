"""Zincir ayrımı regresyonları: migration, zincir kolonu, panel görünümü."""

from __future__ import annotations

import sqlite3

import pandas as pd

from avimsin.storage.db import (
    coin_chains,
    connect,
    save_coin,
    save_purchase,
    set_chain,
    wallet_chains,
)
from avimsin.storage.views import (
    UNKNOWN_CHAIN,
    chain_options,
    filter_by_chain,
    wallet_chain_map,
    with_chain,
)

EVM_COIN = "0x" + "ab" * 20
SOL_COIN = "M" * 44
EVM_WALLET = "0x" + "11" * 20
SOL_WALLET = "S" * 44


# --- migration ---


def test_legacy_db_migrates_in_place_without_data_loss(tmp_path) -> None:
    """chain kolonu eklenmeden önce yazılmış DB açılınca korunur, kolon eklenir."""
    db = tmp_path / "legacy.sqlite"
    raw = sqlite3.connect(db)
    raw.executescript(
        """
        CREATE TABLE coins (address TEXT PRIMARY KEY, first_block INTEGER NOT NULL);
        CREATE TABLE wallets (
            address TEXT PRIMARY KEY, first_seen_block INTEGER NOT NULL,
            is_contract INTEGER, verdict TEXT, verdict_rule TEXT, verdict_reason TEXT
        );
        CREATE TABLE purchases (
            coin TEXT NOT NULL, wallet TEXT NOT NULL, block INTEGER NOT NULL,
            tx TEXT NOT NULL, PRIMARY KEY (coin, tx, wallet)
        );
        CREATE TABLE scores (
            wallet TEXT PRIMARY KEY, winrate REAL NOT NULL, net_pnl INTEGER NOT NULL,
            trades INTEGER NOT NULL, frequency REAL NOT NULL, score REAL NOT NULL
        );
        INSERT INTO coins VALUES ('0xabc', 500);
        INSERT INTO purchases VALUES ('0xabc', '0xdef', 501, 't1');
        """
    )
    raw.commit()
    raw.close()

    conn = connect(db)
    try:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(coins)")}
        assert "chain" in cols
        assert conn.execute("SELECT first_block FROM coins").fetchone() == (500,)
        assert conn.execute("SELECT COUNT(*) FROM purchases").fetchone() == (1,)
        assert conn.execute("SELECT chain FROM coins").fetchone() == (None,)
        # backfill: zincir sonradan tamamlanır, first_block bozulmaz
        save_coin(conn, "0xabc", 999, chain="robinhood")
        assert conn.execute("SELECT first_block, chain FROM coins").fetchone() == (500, "robinhood")
    finally:
        conn.close()


def test_reopening_migrated_db_is_idempotent(tmp_path) -> None:
    db = tmp_path / "x.sqlite"
    connect(db).close()
    conn = connect(db)
    try:
        assert {r[1] for r in conn.execute("PRAGMA table_info(coins)")} >= {"chain"}
        save_coin(conn, EVM_COIN, 10, chain="robinhood")
        conn.commit()
    finally:
        conn.close()
    conn = connect(db)
    try:
        assert conn.execute("SELECT chain FROM coins").fetchone() == ("robinhood",)
    finally:
        conn.close()


# --- zincir kaydı ---


def test_coin_and_wallet_chains_preserve_address_case() -> None:
    conn = connect(":memory:")
    try:
        save_coin(conn, EVM_COIN.upper(), 10, chain="robinhood")
        save_coin(conn, SOL_COIN, 20, chain="solana")
        save_purchase(conn, SOL_COIN, SOL_WALLET, 21, "sig1")
        save_purchase(conn, EVM_COIN, EVM_WALLET.upper(), 11, "0xtx")
        conn.commit()

        assert coin_chains(conn) == {EVM_COIN.lower(): "robinhood", SOL_COIN: "solana"}
        chains = wallet_chains(conn)
        assert chains[SOL_WALLET] == "solana"
        assert chains[EVM_WALLET.lower()] == "robinhood"  # EVM lower, base58 aynen
    finally:
        conn.close()


def test_set_chain_only_fills_missing_value() -> None:
    conn = connect(":memory:")
    try:
        save_coin(conn, EVM_COIN, 10, chain="robinhood")
        save_coin(conn, SOL_COIN, 20)  # zincirsiz kayıt
        set_chain(conn, EVM_COIN, "solana")  # dolu: dokunmaz
        set_chain(conn, SOL_COIN, "solana")  # boş: tamamlar
        conn.commit()
        assert coin_chains(conn) == {EVM_COIN.lower(): "robinhood", SOL_COIN: "solana"}
    finally:
        conn.close()


def test_coin_chains_excludes_unknown() -> None:
    conn = connect(":memory:")
    try:
        save_coin(conn, EVM_COIN, 10)
        conn.commit()
        assert coin_chains(conn) == {}
    finally:
        conn.close()


# --- panel görünümü ---


def purchases_frame() -> pd.DataFrame:
    return pd.DataFrame(
        [
            # Aynı cüzdan iki zincirde: en erken blok kazanır
            {"coin": SOL_COIN, "wallet": EVM_WALLET, "block": 20, "tx": "s1", "chain": "solana"},
            {"coin": EVM_COIN, "wallet": EVM_WALLET, "block": 5, "tx": "e1", "chain": "robinhood"},
            {"coin": EVM_COIN, "wallet": SOL_WALLET, "block": 6, "tx": "e2", "chain": None},
        ]
    )


def test_wallet_chain_map_earliest_and_unknown() -> None:
    mapping = wallet_chain_map(purchases_frame())
    assert mapping[EVM_WALLET] == "robinhood"  # blok 5 < 20
    assert mapping[SOL_WALLET] == UNKNOWN_CHAIN  # NULL → bilinmiyor


def test_wallet_chain_map_on_empty_frame() -> None:
    assert wallet_chain_map(pd.DataFrame()).empty


def test_chain_options_lists_unknown_last() -> None:
    opts = chain_options(with_chain(purchases_frame()))
    assert opts == ["robinhood", "solana", UNKNOWN_CHAIN]


def test_filter_by_chain_selects_and_passes_through() -> None:
    frame = with_chain(purchases_frame())
    only_sol = filter_by_chain("solana", frame)[0]
    assert list(only_sol["tx"]) == ["s1"]

    unknown = filter_by_chain(UNKNOWN_CHAIN, frame)[0]
    assert list(unknown["tx"]) == ["e2"]

    # Tümü: dokunmaz
    (all_rows,) = filter_by_chain(None, frame)
    assert len(all_rows) == 3

    # zincir kolonu olmayan frame filtreden etkilenmez
    (plain,) = filter_by_chain("solana", pd.DataFrame({"a": [1]}))
    assert list(plain["a"]) == [1]


def test_with_chain_fills_nulls_without_mutating_input() -> None:
    frame = purchases_frame()
    filled = with_chain(frame)
    assert filled["chain"].tolist()[-1] == UNKNOWN_CHAIN
    assert frame["chain"].isna().sum() == 1  # orijinal bozulmadı