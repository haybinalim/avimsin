"""avimsin-filter: DB'deki erken alıcıları filtre motorundan geçirir.

Kullanım::

    uv run avimsin-filter <coin-adresi> [--from-block <blok>] [--db <yol>]
"""

from __future__ import annotations

import argparse

from .chains.robinhood import connect
from .collectors.transfers import token_transfers
from .filters.base import VERDICT_BOT, VERDICT_DUMP, VERDICT_OK, WalletHistory
from .filters.bot_rules import ContractRule, HighFrequencyRule, RoundTripRule
from .filters.dump_rules import DumpRule
from .storage.db import connect as db_connect
from .storage.db import save_verdict


def _is_contract(client, wallet: str) -> bool:
    """Cüzdanın kontrat olup olmadığını söyler (eth_getCode)."""
    return client.call("eth_getCode", [wallet, "latest"]) != "0x"


def main() -> None:
    """CLI giriş noktası: tüm kuralları çalıştırır, sonuçları DB'ye yazar."""
    parser = argparse.ArgumentParser(
        description="Erken alıcıları filtre kurallarından geçirir (bot/kaçak satış eleme)."
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
            histories: dict[str, WalletHistory] = {}
            for wallet in wallets:
                relevant = [e for e in events if e.frm == wallet or e.to == wallet]
                histories[wallet] = WalletHistory(
                    wallet=wallet, transfers=relevant, is_contract=_is_contract(client, wallet)
                )

        rules = [ContractRule(), RoundTripRule(), HighFrequencyRule(), DumpRule()]
        counts = {"bot": 0, "dump": 0, VERDICT_OK: 0}
        for wallet, hist in histories.items():
            verdict = VERDICT_OK
            rule_name, reason = "-", "tüm kurallardan geçti"
            for rule in rules:
                result = rule.check(hist)
                if result is not None:
                    verdict, rule_name, reason = result.verdict, result.rule, result.reason
                    break
            counts[verdict] += 1
            save_verdict(conn, wallet, verdict, rule_name, reason)
            print(f"{wallet}  {verdict:<5} [{rule_name}] {reason}")
        conn.commit()

        total = sum(counts.values())
        print(f"\n{total} cüzdan: {counts[VERDICT_OK]} temiz, {counts['bot']} bot, {counts['dump']} kaçak satış")
    finally:
        conn.close()
