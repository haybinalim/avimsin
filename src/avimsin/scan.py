"""avimsin-scan: bir token'ın erken alıcılarını zincirden toplar ve SQLite'a yazar.

Kullanım::

    uv run avimsin-scan <token-adresi> --from-block <blok> --count 50
"""

from __future__ import annotations

import argparse

from .chains.robinhood import connect
from .collectors.early_buyers import earliest_buyers
from .storage.db import connect as db_connect
from .storage.db import save_coin, save_purchase


def main() -> None:
    """CLI giriş noktası."""
    parser = argparse.ArgumentParser(
        description="Bir token'ın erken alıcılarını zincirden toplar ve veritabanına yazar."
    )
    parser.add_argument("token", help="ERC-20 token kontrat adresi")
    parser.add_argument("--from-block", type=int, required=True, help="Taramanın başlayacağı blok")
    parser.add_argument("--count", type=int, default=50, help="Kaç erken alıcı toplanacak")
    parser.add_argument("--db", default="data/avimsin.sqlite", help="SQLite dosya yolu")
    args = parser.parse_args()

    with connect() as client:
        buyers = earliest_buyers(client, args.token, args.from_block, args.count)

    conn = db_connect(args.db)
    try:
        save_coin(conn, args.token, args.from_block)
        for buyer in buyers:
            save_purchase(conn, args.token, buyer.wallet, buyer.block, buyer.tx)
        conn.commit()
    finally:
        conn.close()

    print(f"{len(buyers)} erken alıcı bulundu, {args.db} içine yazıldı:")
    for i, buyer in enumerate(buyers, start=1):
        print(f"{i:>3}. {buyer.wallet}  blok {buyer.block}  {buyer.tx}")
