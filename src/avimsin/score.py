"""avimsin-score: filtrelenmiş cüzdanları transfer verisiyle skorlar ve sıralar.

Sonuçlar coin bazlıdır: uygunluk coin_verdicts'ten (o coin, verdict=ok) okunur,
skorlar coin_scores'ta yalnız bu coin için atomik yenilenir.
Tarama aralığı filter'ın yazdığı kalıcı analiz penceresidir (analysis_windows, bkz.
window.py). score pencereyi yalnız OKUR: genişletmez, yazmaz; kayıt yoksa RPC'den önce
hata verir. Açık --from-block/--to-block kayıtla uyuşmuyorsa hata verir (pencereyi
avimsin-filter değiştirir).

Kullanım::

    uv run avimsin-score <coin-adresi> [--from-block <blok>] [--to-block <blok>] [--db <yol>]
"""

from __future__ import annotations

import argparse
from decimal import Decimal

from .chains import attach_tx_cache, open_chain
from .filters.base import WalletHistory
from .scoring.engine import rank, score_wallet
from .storage.db import connect as db_connect
from .storage.db import (
    LEGACY_NOTE,
    VERDICT_OK,
    check_coin_chain,
    clear_coin_scores,
    has_coin_results,
    load_window,
    normalize_address,
    save_score,
    set_chain,
)


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
        help="Yalnız doğrulama: kayıtlı pencerenin başlangıcıyla aynı olmalı; değilse hata "
        "(pencereyi avimsin-filter değiştirir)",
    )
    parser.add_argument(
        "--to-block", type=int, default=None,
        help="Yalnız doğrulama: kayıtlı pencerenin bitişiyle aynı olmalı; değilse hata "
        "(pencereyi avimsin-filter değiştirir)",
    )
    parser.add_argument(
        "--chain", default=None, choices=["robinhood", "solana"],
        help="Zincir seçimi (varsayılan: kayıtlı zincir, yoksa solana; eski zincirsiz kayıt için zorunlu)",
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
        # Pencereyi YALNIZ filter yazar; score salt-okur (kararlar o aralığa dayanır).
        stored = load_window(conn, coin)
        if stored is None:
            raise SystemExit("Analiz penceresi yok; önce avimsin-filter çalıştırın.")
        if (args.from_block is not None and args.from_block != stored[0]) or (
            args.to_block is not None and args.to_block != stored[1]
        ):
            new_from = args.from_block if args.from_block is not None else stored[0]
            new_to = args.to_block if args.to_block is not None else stored[1]
            raise SystemExit(
                f"Pencere filter ile değiştirilir: avimsin-filter {coin} "
                f"--from-block {new_from} --to-block {new_to} "
                f"(kayıtlı pencere {stored[0]}–{stored[1]})"
            )
        wallets = conn.execute(
            "SELECT DISTINCT p.wallet FROM purchases p WHERE p.coin = ? ORDER BY p.block",
            (coin,),
        ).fetchall()
        if not wallets:
            raise SystemExit(f"{coin} için kayıtlı alıcı yok; önce avimsin-scan çalıştırın.")

        # Uygunluk yalnız bu coin'in kararından gelir (coin_verdicts, verdict=ok).
        ok_wallets = {
            r[0]
            for r in conn.execute(
                "SELECT wallet FROM coin_verdicts WHERE coin = ? AND verdict = ?",
                (coin, VERDICT_OK),
            )
        }
        eligible = [w for (w,) in wallets if w in ok_wallets]
        if not has_coin_results(conn):
            print(LEGACY_NOTE)
        elif conn.execute(
            "SELECT 1 FROM coin_verdicts WHERE coin = ? LIMIT 1", (coin,)
        ).fetchone() is None:
            print(f"NOT: {coin} için coin_verdicts yok; önce avimsin-filter {coin} çalıştırın.")
        state = "tam" if stored[2] else "eksik"
        print(f"Analiz penceresi: {stored[0]}–{stored[1]} ({state})")
        if not stored[2]:
            print(
                "UYARI: pencere eksik; tamamlamak için önce avimsin-filter'ı yeniden çalıştırın"
            )
        scores = []
        if eligible:
            with open_chain(chain) as client:
                wallet_fetch = getattr(client, "wallet_transfers", None)
                if wallet_fetch is not None:
                    # Önbellek filter'ın indirdiği işlemleri yeniden indirtmez (score salt-okur:
                    # pencereye dokunulmaz; DB'de yalnız işlem önbelleği tablosu yazılır).
                    attach_tx_cache(client, args.db)
                    fetched = wallet_fetch(coin, eligible, stored[0], stored[1])
                    events = fetched.events
                    for wallet in sorted(fetched.capped):
                        print(f"NOT: {wallet} pencerede ≥{fetched.limit + 1} işlem; skorlanmadı")
                    eligible = [w for w in eligible if w not in fetched.capped]
                else:
                    events = client.token_transfers(coin, stored[0], stored[1])
                token_unit = 10 ** client.decimals(coin)

            for wallet in eligible:
                relevant = [e for e in events if e.frm == wallet or e.to == wallet]
                result = score_wallet(WalletHistory(wallet=wallet, transfers=relevant))
                if result is not None:
                    scores.append(result)

        # Yalnız bu coin'in coin_scores satırları atomik yenilenir; başka coin'e dokunulmaz.
        with conn:
            set_chain(conn, coin, chain)
            clear_coin_scores(conn, coin)
            for result in scores:
                save_score(
                    conn, coin, result.wallet, result.winrate, result.net_pnl,
                    result.trades, result.frequency, result.score, token_unit,
                )

        ranked = rank(scores)
        print("Transfer proxy: çıkış satış değildir; oran ekonomik winrate, net akış finansal P&L değildir.")
        print(f"Skorlar coin bazlıdır ({coin}); diğer coinlerin skorlarına dokunulmadı.")
        print(f"{'SIRA':<6}{'CUZDAN':<44}{'SCORE':<8}{'ORAN*':<10}{'NET AKIS (token)':>20}{'CIKIS':>8}")
        print("-" * 96)
        for i, s in enumerate(ranked, start=1):
            net_flow = f"{Decimal(s.net_pnl) / Decimal(token_unit):+.2f}"
            print(f"{i:<6}{s.wallet:<44}{s.score:<8}{s.winrate:<10}{net_flow:>20}{s.trades:>8}")
        print(f"\n{len(ranked)} cüzdan skorlandı, {args.db} içine yazıldı.")
    finally:
        conn.close()
