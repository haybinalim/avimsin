"""avimsin-score: filtrelenmiş cüzdanları transfer verisiyle skorlar ve sıralar.

Kullanım::

    uv run avimsin-score <coin-adresi> [--from-block <blok>] [--db <yol>]
"""

from __future__ import annotations

import argparse

from .chains.robinhood import connect
from .collectors.transfers import token_transfers
from .filters.base import WalletHistory
from .scoring.engine import rank, score_wallet
from .storage.db import connect as db_connect
from .storage.db import save_score


def main() -> None:
    """CLI giriş noktası: temiz cüzdanları skorlar, DB'ye yazar, tablo basar."""
    parser = argparse.ArgumentParser(
        description="Filtrelenmiş erken alıcıları winrate/P&L/frekans bazında skorlar."
    )
    parser.add_argument("coin", help="ERC-20 token kontrat adresi")
    parser.add_argument(
        "--from-block",
        type=int,
        default=None,
        help="Transfer taraması başlangıcı (varsayılan: coin'in kayıtlı ilk bloğu)",
    )
    parser.add_argument(
        "--to-block", type=int, default=None, help="Tarama bitişi (varsayılan: en güncel blok)"
    )
    parser.add_argument("--db", default="data/avimsin.sqlite", help="SQLite dosya yolu")
    args = parser.parse_args()

    conn = db_connect(args.db)
    try:
        coin = args.coin.lower()
        if args.from_block is None:
            row = conn.execute(
                "SELECT first_block FROM coins WHERE address = ?", (coin,)
            ).fetchone()
            if row is None:
                raise SystemExit(f"{coin} veritabanında yok; önce avimsin-scan çalıştırın.")
            from_block = row[0]
        else:
            from_block = args.from_block

        wallets = [
            r[0]
            for r in conn.execute(
                "SELECT DISTINCT wallet FROM purchases WHERE coin = ? ORDER BY block", (coin,)
            ).fetchall()
        ]
        if not wallets:
            raise SystemExit(f"{coin} için kayıtlı alıcı yok; önce avimsin-scan çalıştırın.")

        with connect() as client:
            to_block = args.to_block if args.to_block is not None else client.block_number()
            events = token_transfers(client, coin, from_block, to_block)

        scores = []
        for wallet in wallets:
            relevant = [e for e in events if e.frm == wallet or e.to == wallet]
            result = score_wallet(WalletHistory(wallet=wallet, transfers=relevant))
            if result is None:
                continue
            scores.append(result)
            save_score(conn, result.wallet, result.winrate, result.net_pnl, result.trades, result.frequency, result.score)
        conn.commit()

        ranked = rank(scores)
        print(f"{'SIRA':<6}{'CUZDAN':<44}{'SCORE':<8}{'WINRATE':<10}{'P&L':>18}{'TRADES':>8}")
        print("-" * 94)
        for i, s in enumerate(ranked, start=1):
            pnl = f"{s.net_pnl / 10**18:+.4f}"
            print(f"{i:<6}{s.wallet:<44}{s.score:<8}{s.winrate:<10}{pnl:>18}{s.trades:>8}")
        print(f"\n{len(ranked)} cüzdan skorlandı, {args.db} içine yazıldı.")
    finally:
        conn.close()
