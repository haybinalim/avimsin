"""Solana getTransaction önbelleği (SQLite, tablo sol_tx_cache; salt-eklemeli).

Sonlanmış işlem değişmez: bir kez indirilen işlem bir daha indirilmez (filter'ın
indirdiğini score yeniden indirmez; yarım kalan tarama kaldığı yerden sürer).
Yalnız adaptörün okuduğu alanlar saklanır (slot, meta.err, token bakiyeleri, accountKeys).

Ayrı bağlantı kullanır ve her yazımı hemen commit eder: uzun bir taramanın ortasında
kesilse de indirilenler kalır. Çağıran, ana bağlantıda bekleyen yazma işlemi
bırakmamalıdır (SQLite tek yazar).
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path


def slim_transaction(tx: dict) -> dict:
    """getTransaction yanıtından adaptörün kullandığı alanları ayıklar."""
    meta = tx.get("meta") or {}

    def balances(side: str) -> list[dict]:
        return [
            {
                "accountIndex": b.get("accountIndex"),
                "mint": b.get("mint"),
                "owner": b.get("owner"),
                "uiTokenAmount": {"amount": (b.get("uiTokenAmount") or {}).get("amount", "0")},
            }
            for b in (meta.get(side) or [])
        ]

    keys = [
        k.get("pubkey", k) if isinstance(k, dict) else k
        for k in (tx.get("transaction", {}).get("message", {}).get("accountKeys") or [])
    ]
    return {
        "slot": tx.get("slot"),
        "transaction": {"message": {"accountKeys": keys}},
        "meta": {
            "err": meta.get("err"),
            "preTokenBalances": balances("preTokenBalances"),
            "postTokenBalances": balances("postTokenBalances"),
        },
    }


class TxCache:
    def __init__(self, path: str | Path) -> None:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(path, timeout=30)
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS sol_tx_cache (signature TEXT PRIMARY KEY, body TEXT NOT NULL)"
        )
        self._conn.commit()

    def get(self, signature: str) -> dict | None:
        row = self._conn.execute(
            "SELECT body FROM sol_tx_cache WHERE signature = ?", (signature,)
        ).fetchone()
        return json.loads(row[0]) if row else None

    def put(self, signature: str, body: dict) -> None:
        with self._conn:
            self._conn.execute(
                "INSERT OR IGNORE INTO sol_tx_cache (signature, body) VALUES (?, ?)",
                (signature, json.dumps(body, separators=(",", ":"))),
            )

    def close(self) -> None:
        self._conn.close()
