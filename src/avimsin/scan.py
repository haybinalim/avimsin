"""avimsin-scan: bir token'ın erken alıcılarını zincirden toplar ve SQLite'a yazar.

Kullanım::

    uv run avimsin-scan <token-adresi> --from-block <blok> --count 50
"""

from __future__ import annotations

import argparse

from .chains import open_chain
from .collectors.early_buyers import earliest_buyers
from .storage.db import connect as db_connect
from .storage.db import check_coin_chain, save_coin, save_purchase


def main() -> None:
    """CLI giriş noktası."""
    parser = argparse.ArgumentParser(
        description="Bir token'ın erken alıcılarını zincirden toplar ve veritabanına yazar."
    )
    parser.add_argument("token", help="Token adresi (EVM kontrat / SPL mint)")
    parser.add_argument("--from-block", type=int, required=True, help="Taramanın başlayacağı blok")
    parser.add_argument("--count", type=int, default=50, help="Kaç erken alıcı toplanacak")
    parser.add_argument(
        "--chain", default=None, choices=["robinhood", "solana"],
        help="Zincir seçimi (varsayılan: robinhood; eski zincirsiz kayıt için zorunlu)",
    )
    parser.add_argument("--db", default="data/avimsin.sqlite", help="SQLite dosya yolu")
    args = parser.parse_args()

    conn = db_connect(args.db)
    try:
        try:
            chain = check_coin_chain(conn, args.token, args.chain)
        except ValueError as exc:
            raise SystemExit(str(exc)) from exc
        with open_chain(chain) as client:
            buyers = earliest_buyers(client, args.token, args.from_block, args.count)
        with conn:
            save_coin(conn, args.token, args.from_block, chain=chain)
            for buyer in buyers:
                save_purchase(conn, args.token, buyer.wallet, buyer.block, buyer.tx)
    finally:
        conn.close()

    print(f"{len(buyers)} erken alıcı bulundu, {args.db} içine yazıldı:")
    for i, buyer in enumerate(buyers, start=1):
        print(f"{i:>3}. {buyer.wallet}  blok {buyer.block}  {buyer.tx}")
