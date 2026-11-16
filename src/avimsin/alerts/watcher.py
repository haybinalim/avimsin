"""Sinyal izleyici: smart wallet'lara pozitif token girişlerini eşikle karşılaştırır.

Son ``WATCH_SIGNAL_WINDOW_SECONDS`` içinde en az ``threshold`` farklı cüzdana aynı
token girdiyse uyarı üretilir. Transferler swap/alım kanıtı değildir; mint, burn
ve self transferler sayılmaz.

Dayanıklılık (SQLite, zincir anahtarlı):
- ``watch_cursor``: son işlenen blok; yeniden başlatmada kesinti taranır.
- ``watch_inflows``: pencere içindeki girişler (turlar arası birikim).
- ``watch_alerts``: bildirilen cüzdanlar. Aynı küme tekrar gönderilmez; yeni cüzdan
  katılırsa "güncelleme" gider; ilk tam sinyalden ``WATCH_ALERT_COOLDOWN_SECONDS`` sonra coin sıfırlanır.
Teslimat durumdan türetilir: gönderim hatası alert kaydı yazılmadan kalır, sonraki
tur aynı sinyali yeniden dener (pencere dışına düşerse kaybolur — bilinen sınır).
"""

from __future__ import annotations

import json
import math
import sqlite3
import time
from dataclasses import dataclass

import httpx

from ..chains.protocol import ChainClient
from ..collectors.transfers import ZERO_ADDRESS, TransferEvent
from ..config import settings
from ..filters.chains import BLOCK_INTERVAL_SECONDS, normalize_chain
from .telegram import TelegramClient


@dataclass(frozen=True)
class Signal:
    """Eşik aşımı: token giren cüzdanlar ve her birinin ilk blok/slot'u.

    ``new_wallets`` None ise tam sinyal; doluysa daha önce bildirilmiş coin için
    yeni katılan cüzdanları belirten güncelleme.
    """

    coin: str
    wallets: list[str]
    blocks: list[int]
    new_wallets: list[str] | None = None


def inflows_by_coin(
    conn: sqlite3.Connection,
    events: list[TransferEvent],
    smart_wallets: set[str] | None = None,
) -> dict[str, dict[str, tuple[int, int]]]:
    """Uygun girişleri coin → {cüzdan: (ilk blok, son blok)} olarak toplar.

    Elenenler: mint/burn, sıfır/negatif değer, öz-transfer, izlenmeyen coin.
    Coin eşleşmesi olayın ``token`` alanıyla yapılır (EVM kontratı / SPL mint).
    """
    result: dict[str, dict[str, tuple[int, int]]] = {}
    for e in events:
        if not (
            e.value > 0
            and e.frm != ZERO_ADDRESS
            and e.to != ZERO_ADDRESS
            and e.frm != e.to
            and (smart_wallets is None or e.to in smart_wallets)
        ):
            continue
        if conn.execute("SELECT 1 FROM coins WHERE address = ?", (e.token,)).fetchone() is None:
            continue  # izlenen coin değil
        wallets = result.setdefault(e.token, {})
        first, last = wallets.get(e.to, (e.block, e.block))
        wallets[e.to] = (min(first, e.block), max(last, e.block))
    return result


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
    signals = []
    for coin, per_wallet in inflows_by_coin(conn, events, smart_wallets).items():
        wallets = sorted(per_wallet)
        if len(wallets) >= threshold:
            signals.append(
                Signal(coin=coin, wallets=wallets, blocks=[per_wallet[w][0] for w in wallets])
            )
    return signals


def format_signal(signal: Signal, threshold: int) -> str:
    """Sinyali Telegram mesajına çevirir."""
    update = signal.new_wallets is not None
    lines = [
        "🏹 <b>Avımsın sinyali — güncelleme</b>" if update else "🏹 <b>Avımsın sinyali</b>",
        f"Coin: <code>{signal.coin}</code>",
        f"{len(signal.wallets)}/{threshold} smart wallet'a token girişi:",
        "Transfer tabanlı gösterge; doğrulanmış swap/alım değildir.",
    ]
    for w, b in zip(signal.wallets, signal.blocks):
        mark = " YENİ" if update and w in (signal.new_wallets or []) else ""
        lines.append(f"  • <code>{w}</code> (ilk giriş bloğu {b}){mark}")
    return "\n".join(lines)


def _blocks_for(seconds: int, chain: str) -> int:
    """Süreyi zincirin blok/slot sayısına çevirir (zaman damgası RPC'den çekilmez)."""
    return max(1, math.ceil(seconds / BLOCK_INTERVAL_SECONDS[chain]))


def _load_cursor(conn: sqlite3.Connection, chain: str) -> int | None:
    row = conn.execute("SELECT last_block FROM watch_cursor WHERE chain = ?", (chain,)).fetchone()
    return row[0] if row else None


def _save_cursor(conn: sqlite3.Connection, chain: str, block: int) -> None:
    conn.execute(
        "INSERT INTO watch_cursor (chain, last_block) VALUES (?, ?)"
        " ON CONFLICT(chain) DO UPDATE SET last_block = excluded.last_block",
        (chain, block),
    )
    conn.commit()


def _record_inflows(
    conn: sqlite3.Connection,
    chain: str,
    inflows: dict[str, dict[str, tuple[int, int]]],
) -> None:
    for coin, per_wallet in inflows.items():
        for wallet, (first, last) in per_wallet.items():
            conn.execute(
                "INSERT INTO watch_inflows (chain, coin, wallet, first_block, last_block)"
                " VALUES (?, ?, ?, ?, ?)"
                " ON CONFLICT(chain, coin, wallet) DO UPDATE SET"
                " first_block = MIN(first_block, excluded.first_block),"
                " last_block = MAX(last_block, excluded.last_block)",
                (chain, coin, wallet, first, last),
            )


def _pending_signals(
    conn: sqlite3.Connection,
    chain: str,
    latest: int,
    threshold: int,
    window_blocks: int,
    cooldown_blocks: int,
) -> list[Signal]:
    """Pencere + bildirim kaydından gönderilmesi gereken sinyalleri türetir."""
    conn.execute(
        "DELETE FROM watch_inflows WHERE chain = ? AND last_block < ?",
        (chain, latest - window_blocks),
    )
    conn.execute(
        "DELETE FROM watch_alerts WHERE chain = ? AND first_sent_block <= ?",
        (chain, latest - cooldown_blocks),
    )
    conn.commit()
    in_window: dict[str, dict[str, int]] = {}
    for coin, wallet, first in conn.execute(
        "SELECT coin, wallet, first_block FROM watch_inflows WHERE chain = ?", (chain,)
    ):
        in_window.setdefault(coin, {})[wallet] = first
    signals = []
    for coin, per_wallet in sorted(in_window.items()):
        if len(per_wallet) < threshold:
            continue
        wallets = sorted(per_wallet)
        row = conn.execute(
            "SELECT wallets FROM watch_alerts WHERE chain = ? AND coin = ?", (chain, coin)
        ).fetchone()
        new: list[str] | None = None
        if row is not None:
            alerted = set(json.loads(row[0]))
            new = [w for w in wallets if w not in alerted]
            if not new:
                continue  # aynı küme: sessiz
        signals.append(
            Signal(
                coin=coin,
                wallets=wallets,
                blocks=[per_wallet[w] for w in wallets],
                new_wallets=new,
            )
        )
    return signals


def _mark_alerted(conn: sqlite3.Connection, chain: str, signal: Signal, latest: int) -> None:
    row = conn.execute(
        "SELECT wallets FROM watch_alerts WHERE chain = ? AND coin = ?", (chain, signal.coin)
    ).fetchone()
    known = set(json.loads(row[0])) if row else set()
    merged = sorted(known | set(signal.wallets))
    conn.execute(
        "INSERT INTO watch_alerts (chain, coin, first_sent_block, last_sent_block, wallets)"
        " VALUES (?, ?, ?, ?, ?)"
        " ON CONFLICT(chain, coin) DO UPDATE SET"
        " last_sent_block = excluded.last_sent_block, wallets = excluded.wallets",
        (chain, signal.coin, latest, latest, json.dumps(merged)),
    )
    conn.commit()


def _describe_error(exc: Exception) -> str:
    """Hata özeti; Telegram URL'i bot token'ı taşıdığı için mesajı yazdırmaz."""
    if isinstance(exc, httpx.HTTPStatusError):
        return f"HTTP {exc.response.status_code}"
    return type(exc).__name__


class WatchRpcError(RuntimeError):
    """once=True turunda RPC hatası; mesaj ve zincir bağlamı URL/anahtar taşımaz."""


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
    window_seconds: int | None = None,
    cooldown_seconds: int | None = None,
) -> int:
    """Seçilen zincirdeki yeni token girişlerini tarar ve eşik aşımını bildirir.

    Döndürür: başarıyla gönderilen bildirim sayısı (once=True tek tur çalışır).
    Coin'ler ve smart wallet geçmişi yalnız ``chain`` ile seçilir; zinciri
    bilinmeyen (NULL) coin'ler dahil edilmez, adres biçiminden zincir tahmin edilmez.
    ``max_slots_per_poll``: Solana gibi hızlı zincirlerde tek turda geriye
    dönük taranacak en fazla aralık. Pencere aşılırsa tur atlanır, `last_block`
    güncele çekilir ve cursor'a yazılır (eski aralık bir daha denenmez — eksik
    veri sessizce sinyal sayılmaz, log satırıyla görünür). None = sınır yok.
    Cursor ``watch_cursor``'da kalıcıdır; yoksa en son bloktan başlanır.
    Telegram hatası döngüyü düşürmez: log satırı yazılır, bildirim kaydı
    işlenmez ve sinyal sonraki turda yeniden denenir (çift gönderim yok).
    Girişler at-least-once işlenir ve idempotent yazılır (aynı aralık yeniden
    taransa da kayıt çoğalmaz). Cooldown ilk TAM sinyalden sayılır; güncellemeler
    süreyi uzatmaz.
    RPC hatası (block_number ya da token_transfers; retry bütçesi bitince): tur
    hepsi-ya-hiç iptal edilir, kalıcı olmayan girişler geri alınır, cursor ilerlemez,
    log satırına yalnız sınıf adı/durum kodu yazılır (URL/anahtar yok). Bekleyen
    sinyaller yine gönderilir (block_number hatasında ``now`` son cursor'dır).
    Daemon modunda döngü sürer; ``once=True`` ise sinyaller gönderildikten sonra
    ``WatchRpcError`` fırlatılır (CLI sıfır olmayan çıkış kodu verir).
    """
    chain = normalize_chain(chain)
    threshold = threshold if threshold is not None else settings.smart_wallet_signal_threshold
    poll_seconds = poll_seconds if poll_seconds is not None else settings.watch_poll_seconds
    if max_slots_per_poll is None:
        max_slots_per_poll = settings.watch_max_slots_per_poll
    window_blocks = _blocks_for(
        window_seconds if window_seconds is not None else settings.watch_signal_window_seconds,
        chain,
    )
    cooldown_blocks = _blocks_for(
        cooldown_seconds if cooldown_seconds is not None else settings.watch_alert_cooldown_seconds,
        chain,
    )
    sent = 0
    last_block = _load_cursor(conn, chain)
    if last_block is None:
        last_block = client.block_number()
        _save_cursor(conn, chain, last_block)
    else:
        print(f"Cursor'dan devam: {chain} blok {last_block}.")

    rpc_failures = 0
    while True:
        rpc_error: Exception | None = None
        latest = last_block  # block_number düşerse son bilinen cursor
        try:
            latest = client.block_number()
            if latest < last_block:
                print(f"UYARI: {chain} güncel blok {latest} < cursor {last_block}; tarama yok.")
            if latest > last_block:
                if max_slots_per_poll is not None and latest - last_block > max_slots_per_poll:
                    print(
                        f"ATLANDI: {latest - last_block} aralık > {max_slots_per_poll} sınır;"
                        f" {last_block + 1}–{latest} taranmadı, güncele geçildi."
                    )
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
                        events.extend(client.token_transfers(coin, last_block + 1, latest))
                    # İstemciden gelen olaylar da seçili coin ve cüzdan bağlamında kalır.
                    watched_coins = set(coins)
                    events = [e for e in events if e.token in watched_coins]
                    _record_inflows(conn, chain, inflows_by_coin(conn, events, smart_wallets))
                    conn.commit()  # girişler kalıcı; cursor ilerleyebilir
                last_block = latest
                _save_cursor(conn, chain, last_block)
        except Exception as exc:  # RPC hatası: hepsi-ya-hiç, cursor ilerlemez
            conn.rollback()  # kısmi girişler yazılmasın
            rpc_error = exc
            latest = last_block
            rpc_failures += 1
            # URL/anahtar sızmasın: yalnız sınıf adı (HTTP ise durum kodu).
            if rpc_failures <= 3 or rpc_failures % 20 == 0:
                print(
                    f"HATA: {chain} RPC turu başarısız ({_describe_error(exc)});"
                    f" cursor {last_block}'da kaldı, aynı aralık yeniden taranacak"
                    f" (ardışık hata: {rpc_failures})."
                )
        else:
            rpc_failures = 0

        now = max(latest, last_block)
        for signal in _pending_signals(conn, chain, now, threshold, window_blocks, cooldown_blocks):
            text = format_signal(signal, threshold)
            if telegram is not None:
                try:
                    telegram.send_message(text=text)
                except Exception as exc:  # döngü ölmesin; sonraki tur yeniden dener
                    print(
                        f"HATA: Telegram gönderimi başarısız ({_describe_error(exc)});"
                        f" {signal.coin} sonraki turda yeniden denenecek."
                    )
                    continue
            _mark_alerted(conn, chain, signal, now)
            kind = "GÜNCELLEME" if signal.new_wallets is not None else "SİNYAL"
            print(f"{kind}: {signal.coin} — {len(signal.wallets)} smart wallet")
            sent += 1
        if once:
            if rpc_error is not None:
                raise WatchRpcError(f"{chain} RPC turu başarısız") from None
            return sent
        time.sleep(poll_seconds)
