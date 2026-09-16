"""Sinyal izleyici: smart wallet'lara pozitif token girişlerini eşikle karşılaştırır.

En az ``threshold`` farklı cüzdana aynı token girdiyse uyarı üretilir.
Transferler swap/alım kanıtı değildir; mint, burn ve self transferler sayılmaz.
Yalnız mevcut tarama aralığı değerlendirilir; kalıcı tekrar bildirimi engeli yoktur.
"""

from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass

from ..chains.protocol import ChainClient
from ..collectors.transfers import ZERO_ADDRESS, TransferEvent
from ..config import settings
from .telegram import TelegramClient


@dataclass(frozen=True)
class Signal:
    """Eşik aşımı: token giren cüzdanlar ve her birinin ilk blok/slot'u."""

    coin: str
    wallets: list[str]
    blocks: list[int]


def detect_signals(
    conn: sqlite3.Connection,
    events: list[TransferEvent],
    threshold: int,
    smart_wallets: set[str] | None = None,
) -> list[Signal]:
    """Pozitif token girişleri arasında eşik aşan coin'leri bulur; swap çözümlemez.

    Coin eşleşmesi olayın ``token`` alanıyla yapılır (EVM kontratı / SPL mint):
    transferin `frm`'i gönderen cüzlandır, token'ın kendisi değil.
    smart_wallets None ise olaylardaki tüm uygun alıcılar sayılır.
    Aynı cüzdanın tekrarlı girişleri tek sayılır; en erken blok/slot saklanır.
    """
    relevant = [
        e
        for e in events
        if e.value > 0
        and e.frm != ZERO_ADDRESS
        and e.to != ZERO_ADDRESS
        and e.frm != e.to
        and (smart_wallets is None or e.to in smart_wallets)
    ]
    by_coin: dict[str, list[TransferEvent]] = {}
    for e in relevant:
        coin_row = conn.execute(
            "SELECT address FROM coins WHERE address = ?", (e.token,)
        ).fetchone()
        if coin_row is None:
            continue  # izlenen coin değil
        by_coin.setdefault(e.token, []).append(e)

    signals = []
    for coin, coin_events in by_coin.items():
        first_blocks: dict[str, int] = {}
        for e in coin_events:
            first_blocks[e.to] = min(first_blocks.get(e.to, e.block), e.block)
        wallets = sorted(first_blocks)
        if len(wallets) >= threshold:
            signals.append(
                Signal(
                    coin=coin,
                    wallets=wallets,
                    blocks=[first_blocks[wallet] for wallet in wallets],
                )
            )
    return signals


def format_signal(signal: Signal, threshold: int) -> str:
    """Sinyali Telegram mesajına çevirir."""
    lines = [
        f"🏹 <b>Avımsın sinyali</b>",
        f"Coin: <code>{signal.coin}</code>",
        f"{len(signal.wallets)}/{threshold} smart wallet'a token girişi:",
        "Transfer tabanlı gösterge; doğrulanmış swap/alım değildir.",
    ]
    for w, b in zip(signal.wallets, signal.blocks):
        lines.append(f"  • <code>{w}</code> (blok {b})")
    return "\n".join(lines)


def watch_loop(
    client: ChainClient,
    conn: sqlite3.Connection,
    token_rpc: ChainClient | None = None,
    threshold: int | None = None,
    poll_seconds: int | None = None,
    telegram: TelegramClient | None = None,
    once: bool = False,
    max_slots_per_poll: int | None = None,
    chain: str = "robinhood",
) -> int:
    """Seçilen zincirdeki yeni token girişlerini tarar ve eşik aşımını bildirir.

    Döndürür: gönderilen bildirim sayısı (once=True tek tur çalışır).
    Coin'ler ve smart wallet geçmişi yalnız ``chain`` ile seçilir; zinciri
    bilinmeyen (NULL) coin'ler dahil edilmez, adres biçiminden zincir tahmin edilmez.
    ``max_slots_per_poll``: Solana gibi hızlı zincirlerde tek turda geriye
    dönük taranacak en fazla aralık. Pencere aşılırsa tur atlanır, `last_block`
    güncele çekilir (eski aralık bir daha denenmez — eksik veri sessizce
    sinyal sayılmaz, log satırıyla görünür). None = sınır yok (EVM varsayılanı).
    """
    threshold = threshold if threshold is not None else settings.smart_wallet_signal_threshold
    poll_seconds = poll_seconds if poll_seconds is not None else settings.watch_poll_seconds
    if max_slots_per_poll is None:
        max_slots_per_poll = settings.watch_max_slots_per_poll
    sent = 0
    last_block = client.block_number()

    while True:
        latest = client.block_number()
        if latest > last_block:
            if max_slots_per_poll is not None and latest - last_block > max_slots_per_poll:
                print(
                    f"ATLANDI: {latest - last_block} aralık > {max_slots_per_poll} sınır;"
                    f" {last_block + 1}–{latest} taranmadı, güncele geçildi."
                )
                last_block = latest
            else:
                # Yalnız açıkça seçilen zincirin coin'lerini sorgula.
                coins = [
                    r[0]
                    for r in conn.execute(
                        "SELECT address FROM coins WHERE chain = ?", (chain,)
                    ).fetchall()
                ]
                smart_wallets = {
                    r[0]
                    for r in conn.execute(
                        "SELECT DISTINCT p.wallet FROM purchases p"
                        " JOIN coins c ON c.address = p.coin"
                        " JOIN wallets w ON w.address = p.wallet"
                        " WHERE c.chain = ? AND w.verdict = 'ok'",
                        (chain,),
                    ).fetchall()
                }
                events: list[TransferEvent] = []
                for coin in coins:
                    events.extend(
                        client.token_transfers(coin, last_block + 1, latest)
                    )
                # İstemciden gelen olaylar da seçili coin ve cüzdan bağlamında kalır.
                watched_coins = set(coins)
                events = [e for e in events if e.token in watched_coins]

                for signal in detect_signals(conn, events, threshold, smart_wallets):
                    text = format_signal(signal, threshold)
                    if telegram is not None:
                        telegram.send_message(text=text)
                    print(f"SİNYAL: {signal.coin} — {len(signal.wallets)} smart wallet")
                    sent += 1
                last_block = latest
        if once:
            return sent
        time.sleep(poll_seconds)
