"""avimsin-score: filtrelenmiş cüzdanları transfer verisiyle skorlar ve sıralar.

Kullanım::

    uv run avimsin-score <coin-adresi> [--from-block <blok>] [--db <yol>]
"""

from __future__ import annotations

import argparse
from decimal import Decimal

from .chains import open_chain
from .filters.base import WalletHistory
from .scoring.engine import rank, score_wallet
from .storage.db import connect as db_connect
from .storage.db import VERDICT_OK, check_coin_chain, normalize_address, save_score, set_chain


def main() -> None:
    """CLI giriş noktası: temiz cüzdanları skorlar, DB'ye yazar, tablo basar."""
    parser = argparse.ArgumentParser(
        description="Temiz erken alıcıları token net-akış proxy'siyle skorlar; finansal P&L hesaplamaz."
    )
    parser.add_argument("coin", help="Token adresi (EVM kontrat / SPL mint)")
    parser.add_argument(
        "--from-block",
        type=int,
        default=None,
        help="Transfer taraması başlangıcı (varsayılan: coin'in kayıtlı ilk bloğu)",
    )
    parser.add_argument(
        "--to-block", type=int, default=None, help="Tarama bitişi (varsayılan: en güncel blok)"
    )
    parser.add_argument(
        "--chain", default=None, choices=["robinhood", "solana"],
        help="Zincir seçimi (varsayılan: robinhood; eski zincirsiz kayıt için zorunlu)",
    )
    parser.add_argument("--db", default="data/avimsin.sqlite", help="SQLite dosya yolu")
    args = parser.parse_args()

    conn = db_connect(args.db)
    try:
        coin = normalize_address(args.coin)
        try:
            chain = check_coin_chain(conn, coin, args.chain)
        except ValueError as exc:
            raise SystemExit(str(exc)) from exc
        if args.from_block is None:
            row = conn.execute(
                "SELECT first_block FROM coins WHERE address = ?", (coin,)
            ).fetchone()
            if row is None:
                raise SystemExit(f"{coin} veritabanında yok; önce avimsin-scan çalıştırın.")
            from_block = row[0]
        else:
            from_block = args.from_block

        wallets = conn.execute(
            "SELECT DISTINCT p.wallet, w.verdict FROM purchases p"
            " JOIN wallets w ON w.address = p.wallet WHERE p.coin = ? ORDER BY p.block",
            (coin,),
        ).fetchall()
        if not wallets:
            raise SystemExit(f"{coin} için kayıtlı alıcı yok; önce avimsin-scan çalıştırın.")

        eligible = [wallet for wallet, verdict in wallets if verdict == VERDICT_OK]
        scores = []
        if eligible:
            with open_chain(chain) as client:
                to_block = args.to_block if args.to_block is not None else client.block_number()
                events = client.token_transfers(coin, from_block, to_block)
                token_unit = 10 ** client.decimals(coin)

            for wallet in eligible:
                relevant = [e for e in events if e.frm == wallet or e.to == wallet]
                result = score_wallet(WalletHistory(wallet=wallet, transfers=relevant))
                if result is not None:
                    scores.append(result)

        # Global şemada coin kaynağı yok: sadece bu coin'in alıcılarına dokunur,
        # ancak ortak cüzdanın başka coin'den gelen skorunu ayıramayız (Plan B).
        with conn:
            set_chain(conn, coin, chain)
            conn.execute(
                "DELETE FROM scores WHERE wallet IN (SELECT wallet FROM purchases WHERE coin = ?)",
                (coin,),
            )
            for result in scores:
                save_score(
                    conn, result.wallet, result.winrate, result.net_pnl,
                    result.trades, result.frequency, result.score, token_unit,
                )

        ranked = rank(scores)
        print("Transfer proxy: çıkış satış değildir; oran ekonomik winrate, net akış finansal P&L değildir.")
        print("Skorlar global cüzdan kaydıdır; ortak cüzdanlarda coin sonuç izolasyonu yoktur.")
        print(f"{'SIRA':<6}{'CUZDAN':<44}{'SCORE':<8}{'ORAN*':<10}{'NET AKIS (token)':>20}{'CIKIS':>8}")
        print("-" * 96)
        for i, s in enumerate(ranked, start=1):
            net_flow = f"{Decimal(s.net_pnl) / Decimal(token_unit):+.2f}"
            print(f"{i:<6}{s.wallet:<44}{s.score:<8}{s.winrate:<10}{net_flow:>20}{s.trades:>8}")
        print(f"\n{len(ranked)} cüzdan skorlandı, {args.db} içine yazıldı.")
    finally:
        conn.close()
