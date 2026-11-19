"""avimsin-filter: DB'deki erken alıcıları filtre motorundan geçirir.

Kararlar coin bazlıdır (coin_verdicts); yalnız verilen coin'in satırları yazılır.
Tarama aralığı kalıcı analiz penceresidir (analysis_windows, bkz. window.py);
pencereyi yalnız filter oluşturur/genişletir/günceller, score aynı kaydı salt-okur kullanır. İlk çalıştırmada ANALYSIS_WINDOW_SECONDS (24 saat) kadar
blok/slot hedeflenir; süresi dolmamışsa eksik kaydedilir ve sonra tamamlanır.

Kullanım::

    uv run avimsin-filter <coin-adresi> [--from-block <blok>] [--to-block <blok>] [--db <yol>]
"""

from __future__ import annotations

import argparse

from .chains import attach_tx_cache, open_chain, report_unreadable
from .chains.protocol import ChainClient
from .filters.base import VERDICT_BOT, VERDICT_DUMP, VERDICT_OK, WalletHistory
from .filters.bot_rules import ContractRule, HighFrequencyRule, RoundTripRule
from .filters.dump_rules import DumpRule
from .storage.db import connect as db_connect
from .storage.db import (
    LEGACY_NOTE,
    check_coin_chain,
    has_coin_results,
    load_window,
    normalize_address,
    save_verdict,
    save_window,
    set_chain,
)
from .window import resolve_window


# Alıcıların çoğu için pencerede işlem yoksa basılan uyarının başı; avimsin-pipeline çıktıyı
# bu şablondan türettiği regex ile okur (done_warn) — metni burada değiştirmek yeterli.
SILENT_WARNING_HEAD = "UYARI: {silent}/{total} alıcının ATA'sında pencerede hiç işlem yok"


def _is_contract(client: ChainClient, wallet: str) -> bool:
    """Cüzdanın kontrat/program olup olmadığını söyler."""
    return client.is_contract(wallet)


def main() -> None:
    """CLI giriş noktası: tüm kuralları çalıştırır, sonuçları DB'ye yazar."""
    parser = argparse.ArgumentParser(
        description="Erken alıcıları transfer tabanlı bot/hızlı çıkış kurallarıyla filtreler; satış kanıtlamaz."
    )
    parser.add_argument("coin", help="Token adresi (EVM kontrat / SPL mint)")
    parser.add_argument(
        "--from-block",
        type=int,
        default=None,
        help="Transfer taraması başlangıcı; verilirse kalıcı pencereyi günceller "
        "(varsayılan: kayıtlı pencere, yoksa coin'in ilk bloğu). --to-block verilmezse "
        "bitiş yeni başlangıçtan yeniden hesaplanır (24 saatlik hedef), eski bitiş korunmaz",
    )
    parser.add_argument(
        "--to-block", type=int, default=None,
        help="Tarama bitişi; verilirse kalıcı pencereyi günceller, en güncel bloktan büyükse hata "
        "(varsayılan: kalıcı pencere; yoksa ANALYSIS_WINDOW_SECONDS uzunluğunda)",
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
        wallets = [
            r[0]
            for r in conn.execute(
                "SELECT DISTINCT wallet FROM purchases WHERE coin = ? ORDER BY block", (coin,)
            ).fetchall()
        ]
        if not wallets:
            raise SystemExit(f"{coin} için kayıtlı alıcı yok; önce avimsin-scan çalıştırın.")

        if not has_coin_results(conn):
            print(LEGACY_NOTE)

        capped: set[str] = set()
        capped_limit = 0
        signature_counts: dict[str, int] | None = None
        with open_chain(chain) as client:
            old_window = load_window(conn, coin)
            window = resolve_window(
                conn, coin, chain, client.block_number, args.from_block, args.to_block
            )
            for line in window.notes:
                print(line)
            wallet_fetch = getattr(client, "wallet_transfers", None)
            histories: dict[str, WalletHistory] = {}
            try:
                if wallet_fetch is not None:
                    # İşlem önbelleği ayrı bağlantıyla yazar (SQLite tek yazar): ana bağlantıdaki
                    # bekleyen pencere yazımı önce commit edilir; hata olursa aşağıda geri alınır.
                    conn.commit()
                    attach_tx_cache(client, args.db)
                    purchase_txs: dict[str, list[str]] = {}
                    for w, t in conn.execute("SELECT wallet, tx FROM purchases WHERE coin = ?", (coin,)):
                        purchase_txs.setdefault(w, []).append(t)
                    fetched = wallet_fetch(
                        coin, wallets, window.from_block, window.to_block, purchase_txs
                    )
                    events = fetched.events
                    capped, capped_limit = fetched.capped, fetched.limit
                    signature_counts = fetched.signature_counts
                    report_unreadable(client)
                else:
                    events = client.token_transfers(coin, window.from_block, window.to_block)
                for wallet in wallets:
                    if wallet in capped:
                        histories[wallet] = WalletHistory(wallet=wallet)
                        continue
                    relevant = [e for e in events if e.frm == wallet or e.to == wallet]
                    histories[wallet] = WalletHistory(
                        wallet=wallet, transfers=relevant, is_contract=_is_contract(client, wallet)
                    )
            except BaseException:
                if wallet_fetch is not None:
                    # Pencere yalnız başarılı bir çalıştırmayla kalıcı olsun (eski davranış: rollback).
                    if old_window is None:
                        conn.execute("DELETE FROM analysis_windows WHERE coin = ?", (coin,))
                    else:
                        save_window(conn, coin, *old_window)
                    conn.commit()
                raise

        if signature_counts is not None:
            silent = sum(1 for w in wallets if signature_counts.get(w, 0) == 0)
            if silent * 2 > len(wallets):
                print(
                    SILENT_WARNING_HEAD.format(silent=silent, total=len(wallets)) + "; "
                    "ATA türetimi ya da pencere hatalı olabilir, sonuçlara güvenmeyin."
                )

        rules = [
            ContractRule(),
            RoundTripRule(chain=chain),
            HighFrequencyRule(),
            DumpRule(chain=chain),
        ]
        counts = {VERDICT_BOT: 0, VERDICT_DUMP: 0, VERDICT_OK: 0, None: 0}
        with conn:
            set_chain(conn, coin, chain)
            for wallet, hist in histories.items():
                verdict = VERDICT_OK if hist.transfers else None
                rule_name = "-"
                reason = "tüm kurallardan geçti" if hist.transfers else "pencerede transfer yok; bilinmiyor"
                if wallet in capped:
                    verdict, rule_name = VERDICT_BOT, HighFrequencyRule.name
                    reason = f"pencerede ≥{capped_limit + 1} işlem (ATA imzaları; işlemler açılmadı)"
                elif hist.transfers:
                    for rule in rules:
                        result = rule.check(hist)
                        if result is not None:
                            verdict, rule_name, reason = result.verdict, result.rule, result.reason
                            break
                counts[verdict] += 1
                save_verdict(conn, coin, wallet, verdict, rule_name, reason)
                print(f"{wallet}  {verdict or 'unknown':<7} [{rule_name}] {reason}")

        total = sum(counts.values())
        print(
            f"\n{total} cüzdan: {counts[VERDICT_OK]} temiz, {counts[VERDICT_BOT]} bot, "
            f"{counts[VERDICT_DUMP]} hızlı çıkış, {counts[None]} bilinmiyor"
        )
        print("Transfer çıkışı satış/zarar kanıtı değildir; filtre transfer tabanlı bir proxy'dir.")
    finally:
        conn.close()
