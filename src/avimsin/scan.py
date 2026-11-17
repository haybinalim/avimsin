"""avimsin-scan: bir token'ın erken alıcılarını zincirden toplar ve SQLite'a yazar.

Kullanım::

    uv run avimsin-scan <token-adresi> [--from-block <blok>] --count 50

Solana'da --from-block opsiyoneldir: verilmezse başlangıç mint'in oluşturulduğu
(en eski işlem) slottur (ikili arama); EVM'de zorunludur.
"""

from __future__ import annotations

import argparse

from .chains import attach_tx_cache, open_chain
from .collectors.early_buyers import DistributionSkips, earliest_buyers
from .storage.db import connect as db_connect
from .storage.db import check_coin_chain, save_coin, save_purchase


def main() -> None:
    """CLI giriş noktası."""
    parser = argparse.ArgumentParser(
        description="Bir token'ın erken alıcılarını zincirden toplar ve veritabanına yazar."
    )
    parser.add_argument("token", help="Token adresi (EVM kontrat / SPL mint)")
    parser.add_argument(
        "--from-block", type=int, default=None,
        help="Taramanın başlayacağı blok (EVM için zorunlu; Solana'da verilmezse mint'in oluşturulduğu slot)",
    )
    parser.add_argument("--count", type=int, default=50, help="Kaç erken alıcı toplanacak")
    parser.add_argument(
        "--chain", default=None, choices=["robinhood", "solana"],
        help="Zincir seçimi (varsayılan: kayıtlı zincir, yoksa solana; eski zincirsiz kayıt için zorunlu)",
    )
    parser.add_argument("--db", default="data/avimsin.sqlite", help="SQLite dosya yolu")
    args = parser.parse_args()

    conn = db_connect(args.db)
    try:
        try:
            chain = check_coin_chain(conn, args.token, args.chain)
        except ValueError as exc:
            raise SystemExit(str(exc)) from exc
        if args.from_block is None and chain != "solana":
            raise SystemExit("--from-block bu zincir için zorunlu (yalnız Solana'da opsiyonel).")
        with open_chain(chain) as client:
            attach_tx_cache(client, args.db)
            from_block = args.from_block
            if from_block is None:
                from_block = client.first_slot(args.token)
                if from_block is None:
                    raise SystemExit(f"{args.token} için işlem bulunamadı; mint adresi doğru mu?")
                print(f"Başlangıç slotu (mint oluşturma): {from_block}")
            skips = DistributionSkips()
            buyers = earliest_buyers(client, args.token, from_block, args.count, skips)
        with conn:
            save_coin(conn, args.token, from_block, chain=chain)
            for buyer in buyers:
                save_purchase(conn, args.token, buyer.wallet, buyer.block, buyer.tx)
    finally:
        conn.close()

    if skips.txs:
        print(f"{skips.receivers} dağıtım alıcısı atlandı ({skips.txs} işlem)")
    print(f"{len(buyers)} erken alıcı bulundu, {args.db} içine yazıldı:")
    for i, buyer in enumerate(buyers, start=1):
        print(f"{i:>3}. {buyer.wallet}  blok {buyer.block}  {buyer.tx}")
