"""Sinyal izleyici: yeni blokları tarar, smart wallet alımlarını eşikle karşılaştırır.

Sinyal mantığı: verilen smart wallet listesinde (verdict=ok) en az `threshold`
farklı cüzdan aynı coini aldıysa uyarı üretilir. Bildirim Telegram'a gider;
aynı (coin, wallet) çifti tekrar bildirim üretmez (purchases PK zaten tekil).
"""

from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass

from ..chains.protocol import ChainClient
from ..collectors.transfers import TransferEvent
from ..config import settings
from .telegram import TelegramClient


@dataclass(frozen=True)
class Signal:
    """Eşik aşımı: bir coin için kaç smart wallet aldı."""

    coin: str
    wallets: list[str]
    blocks: list[int]


def detect_signals(
    conn: sqlite3.Connection,
    events: list[TransferEvent],
    threshold: int,
    smart_wallets: set[str] | None = None,
) -> list[Signal]:
    """Yeni alımlar arasında eşik aşan coin'leri bulur.

    Coin eşleşmesi olayın ``token`` alanıyla yapılır (EVM kontratı / SPL mint):
    transferin `frm`'i gönderen cüzlandır, token'ın kendisi değil.
    smart_wallets None ise purchases tablosundaki tüm cüzdanlar sayılır
    (verdict filtresi CLI tarafında uygulanır).
    """
    relevant = [
        e
        for e in events
        if smart_wallets is None or e.to in smart_wallets
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
        wallets = {e.to for e in coin_events}
        if len(wallets) >= threshold:
            signals.append(
                Signal(
                    coin=coin,
                    wallets=sorted(wallets),
                    blocks=[e.block for e in coin_events],
                )
            )
    return signals


def format_signal(signal: Signal, threshold: int) -> str:
    """Sinyali Telegram mesajına çevirir."""
    lines = [
        f"🏹 <b>Avımsın sinyali</b>",
        f"Coin: <code>{signal.coin}</code>",
        f"{len(signal.wallets)}/{threshold} smart wallet aldı:",
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
) -> int:
    """İzleme döngüsü: yeni bloklardaki alımları tarar, sinyal üretilirse bildirir.

    Döndürür: gönderilen bildirim sayısı (once=True tek tur çalışır).
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
                # İzlenen coin'lerin alımlarını yeni bloklardan çek
                coins = [r[0] for r in conn.execute("SELECT address FROM coins").fetchall()]
                smart_wallets = {
                    r[0]
                    for r in conn.execute(
                        "SELECT DISTINCT wallet FROM purchases WHERE wallet IN"
                        " (SELECT address FROM wallets WHERE verdict = 'ok')"
                    ).fetchall()
                }
                events: list[TransferEvent] = []
                for coin in coins:
                    events.extend(
                        client.token_transfers(coin, last_block + 1, latest)
                    )
                # Sadece smart wallet alımları sinyal adayı; coin eşleşmesini
                # detect_signals olayın token alanıyla yapar.
                events = [e for e in events if e.to in smart_wallets]

                for signal in detect_signals(conn, events, threshold):
                    text = format_signal(signal, threshold)
                    if telegram is not None:
                        telegram.send_message(text=text)
                    print(f"SİNYAL: {signal.coin} — {len(signal.wallets)} smart wallet")
                    sent += 1
                last_block = latest
        if once:
            return sent
        time.sleep(poll_seconds)
