"""Sinyal izleyici: smart wallet'lara pozitif token girişlerini eşikle karşılaştırır.

Son ``WATCH_SIGNAL_WINDOW_SECONDS`` içinde en az ``threshold`` farklı cüzdana aynı
token girdiyse uyarı üretilir. Transferler swap/alım kanıtı değildir; mint, burn
ve self transferler sayılmaz.

Dayanıklılık (SQLite, zincir anahtarlı):
- ``watch_cursor``: son işlenen blok; yeniden başlatmada kesinti taranır.
- ``watch_inflows``: pencere içindeki girişler (turlar arası birikim).
- ``watch_alerts``: bildirilen cüzdanlar. Aynı küme tekrar gönderilmez; yeni cüzdan
  katılırsa "güncelleme" gider; ilk tam sinyalden ``WATCH_ALERT_COOLDOWN_SECONDS`` sonra coin sıfırlanır.
Smart wallet kümesi coin bazlı kararlardan (``coin_verdicts``) türetilir:
``smart_wallets_for_chain``. Küme boşsa uyarı yazılır (ilk tur, dolu→boş geçişi).
Solana varsayılanı cüzdan bazlıdır (``watch_wallet_cursor``): her tur her smart wallet'ın
yalnız yeni imzaları açılır; cüzdanın sahibi olduğu token bakiyesi artan HER mint giriştir
(coins tablosunda olması gerekmez; wSOL/USDC/USDT hariç). Girişler doğrudan ``watch_inflows``'a
yazılır; ``_pending_signals`` coins tablosuna bakmaz, bu yüzden coins'e mint eklemek gerekmez.
Cüzdan hatasında yalnız o cüzdanın cursor'ı ilerlemez. Eski coin bazlı yol EVM'de ve
``WATCH_SOLANA_MODE=coin``'de sürer.
Teslimat durumdan türetilir: gönderim hatası alert kaydı yazılmadan kalır, sonraki
tur aynı sinyali yeniden dener (pencere dışına düşerse kaybolur — bilinen sınır).
"""

from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass
from typing import Any

import httpx

from ..chains.protocol import ChainClient
from ..collectors.transfers import ZERO_ADDRESS, TransferEvent
from ..config import settings
from ..filters.chains import blocks_for_seconds, normalize_chain
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


def format_signal(signal: Signal, threshold: int, chain: str | None = None) -> str:
    """Sinyali Telegram mesajına çevirir (Solana'da DexScreener bağlantısı eklenir)."""
    update = signal.new_wallets is not None
    lines = [
        "🏹 <b>Avımsın sinyali — güncelleme</b>" if update else "🏹 <b>Avımsın sinyali</b>",
        f"Coin: <code>{signal.coin}</code>",
        f"{len(signal.wallets)}/{threshold} smart wallet'a token girişi:",
        "Transfer tabanlı gösterge; doğrulanmış swap/alım değildir.",
    ]
    if chain == "solana":
        lines.insert(2, f"https://dexscreener.com/solana/{signal.coin}")
    for w, b in zip(signal.wallets, signal.blocks):
        mark = " YENİ" if update and w in (signal.new_wallets or []) else ""
        lines.append(f"  • <code>{w}</code> (ilk giriş bloğu {b}){mark}")
    return "\n".join(lines)


def _blocks_for(seconds: int, chain: str) -> int:
    """Süreyi zincirin blok/slot sayısına çevirir (zaman damgası RPC'den çekilmez)."""
    return blocks_for_seconds(seconds, chain)


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


def smart_wallets_for_chain(conn: sqlite3.Connection, chain: str) -> set[str]:
    """Seçili zincirde en az bir coin'de verdict=ok ve hiçbir coin'de verdict=bot olmayan cüzdanlar.

    Karar coin bazlıdır (coin_verdicts). dump coin'e özgüdür, tek başına dışlamaz;
    bot ise cüzdanın niteliğidir, herhangi bir coin'de bot olan dışlanır. NULL zincirli
    coin'ler ve başka zincirin kararları hesaba katılmaz.
    """
    return {
        r[0]
        for r in conn.execute(
            "SELECT DISTINCT v.wallet FROM coin_verdicts v"
            " JOIN coins c ON c.address = v.coin"
            " WHERE c.chain = ? AND v.verdict = 'ok'"
            " AND v.wallet NOT IN ("
            "   SELECT b.wallet FROM coin_verdicts b JOIN coins bc ON bc.address = b.coin"
            "   WHERE bc.chain = ? AND b.verdict = 'bot')",
            (chain, chain),
        )
    }


def tx_inflow_mints(tx: dict, wallet: str, ignored: frozenset[str] | set[str]) -> set[str]:
    """İşlemde ``wallet``'ın sahibi olduğu token bakiyesi artan mint'ler (yoksa boş).

    meta.err doluysa boş; owner == wallet olan hesaplar mint bazında toplanır
    (post - pre > 0); ``ignored`` (wSOL/USDC/USDT) sayılmaz.
    """
    meta = tx.get("meta") or {}
    if meta.get("err") is not None:
        return set()
    delta: dict[str, int] = {}
    for side, sign in (("preTokenBalances", -1), ("postTokenBalances", 1)):
        for b in meta.get(side) or []:
            if b.get("owner") != wallet:
                continue
            mint = b.get("mint")
            amount = int((b.get("uiTokenAmount") or {}).get("amount") or 0)
            delta[mint] = delta.get(mint, 0) + sign * amount
    return {m for m, d in delta.items() if d > 0 and m not in ignored}


@dataclass
class WalletRound:
    """Cüzdan bazlı turun özeti."""

    wallets: int = 0
    new_tx: int = 0
    inflows: int = 0
    errors: int = 0


def _load_wallet_cursor(conn: sqlite3.Connection, chain: str, wallet: str) -> str | None:
    row = conn.execute(
        "SELECT last_signature FROM watch_wallet_cursor WHERE chain = ? AND wallet = ?",
        (chain, wallet),
    ).fetchone()
    return row[0] if row else None


def _save_wallet_cursor(
    conn: sqlite3.Connection, chain: str, wallet: str, signature: str, slot: int
) -> None:
    conn.execute(
        "INSERT INTO watch_wallet_cursor (chain, wallet, last_signature, last_slot)"
        " VALUES (?, ?, ?, ?)"
        " ON CONFLICT(chain, wallet) DO UPDATE SET"
        " last_signature = excluded.last_signature, last_slot = excluded.last_slot",
        (chain, wallet, signature, slot),
    )


def _scan_wallet(
    client: Any,
    conn: sqlite3.Connection,
    chain: str,
    wallet: str,
    max_tx: int,
    ignored: frozenset[str],
    stats: WalletRound,
) -> None:
    """Tek cüzdanı tarar. RPC hatası fırlatır; kalıcı yazım yalnız başarıda yapılır."""
    cursor = _load_wallet_cursor(conn, chain, wallet)
    if cursor is None:
        # İlk görülüş: geriye dönük tarama yok; cursor en yeni imzaya.
        newest = client.new_signatures(wallet, None, 1)
        sig, slot = (newest[0]["signature"], int(newest[0]["slot"])) if newest else ("", 0)
        _save_wallet_cursor(conn, chain, wallet, sig, slot)
        conn.commit()
        print(
            f"ilk görülen cüzdan {wallet}: cursor {sig[:8] or '(işlem yok)'},"
            " geriye dönük tarama yok.",
            flush=True,
        )
        return
    entries = client.new_signatures(wallet, cursor or None, max_tx + 1)
    if not entries:
        return
    if len(entries) > max_tx:
        print(
            f"UYARI: {wallet} için yeni işlem > {max_tx}; en yeni {max_tx} açılır, kalanlar atlandı.",
            flush=True,
        )
        entries = entries[:max_tx]
    found: dict[str, int] = {}  # mint -> en erken slot
    opened = 0
    for entry in entries:  # en yeni önce
        if entry.get("err") is not None:
            continue
        tx = client.get_transaction(entry["signature"])
        if tx is None:
            raise RuntimeError(f"işlem okunamadı: {entry['signature'][:8]}")
        opened += 1
        slot = int(entry["slot"])
        for mint in tx_inflow_mints(tx, wallet, ignored):
            found[mint] = min(found.get(mint, slot), slot)
    # Hepsi-ya-hiç: buraya geldiyse cüzdanın tüm işlemleri okundu.
    _record_inflows(conn, chain, {m: {wallet: (sl, sl)} for m, sl in found.items()})
    _save_wallet_cursor(conn, chain, wallet, entries[0]["signature"], int(entries[0]["slot"]))
    conn.commit()
    stats.new_tx += opened
    stats.inflows += len(found)


def scan_wallets(
    client: Any, conn: sqlite3.Connection, chain: str, wallets: set[str], max_tx: int
) -> WalletRound:
    """Her smart wallet'ı sırayla tarar; cüzdan hatası yalnız o cüzdanı geri bırakır."""
    ignored = frozenset(settings.watch_ignored_mints)
    stats = WalletRound(wallets=len(wallets))
    for wallet in sorted(wallets):
        try:
            _scan_wallet(client, conn, chain, wallet, max_tx, ignored, stats)
        except Exception as exc:
            conn.rollback()
            stats.errors += 1
            print(
                f"HATA: {wallet} taranamadı ({_describe_error(exc)}); cursor ilerlemedi.",
                flush=True,
            )
    return stats


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
    wallet_mode: bool | None = None,
    max_tx_per_wallet: int | None = None,
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
    Cüzdan modu (``wallet_mode``; None = Solana'da ``WATCH_SOLANA_MODE`` != coin ve istemci
    ``new_signatures`` sunuyorsa): bkz. modül açıklaması; ``max_tx_per_wallet`` cüzdan başına tur
    sınırıdır (varsayılan ``WATCH_MAX_TX_PER_WALLET_POLL``). Tüm cüzdanlar hata verirse tur
    RPC hatası sayılır. Her tur tek özet satırı basar.
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
    if wallet_mode is None:
        wallet_mode = (
            chain == "solana"
            and settings.watch_solana_mode != "coin"
            and hasattr(client, "new_signatures")
        )
    if max_tx_per_wallet is None:
        max_tx_per_wallet = settings.watch_max_tx_per_wallet_poll
    sent = 0
    last_block = _load_cursor(conn, chain)
    if last_block is None:
        last_block = client.block_number()
        _save_cursor(conn, chain, last_block)
    else:
        print(f"Cursor'dan devam: {chain} blok {last_block}.")

    rpc_failures = 0
    smart_known: bool | None = None  # son turda smart wallet kümesi doluydu mu (None: henüz bakılmadı)
    while True:
        rpc_error: Exception | None = None
        latest = last_block  # block_number düşerse son bilinen cursor
        round_started = time.monotonic()
        try:
            latest = client.block_number()
            if wallet_mode:
                smart_wallets = smart_wallets_for_chain(conn, chain)
                if not smart_wallets and smart_known is not False:
                    print(
                        f"UYARI: {chain} için smart wallet yok; avimsin-filter ve"
                        " avimsin-score'u coin bazında yeniden çalıştırın.",
                        flush=True,
                    )
                smart_known = bool(smart_wallets)
                stats = scan_wallets(client, conn, chain, smart_wallets, max_tx_per_wallet)
                if latest > last_block:
                    last_block = latest
                    _save_cursor(conn, chain, last_block)
                note = " (smart wallet kümesi boş)" if not smart_wallets else ""
                print(
                    f"tur: {stats.wallets} cüzdan, {stats.new_tx} yeni işlem,"
                    f" {stats.inflows} giriş, {stats.errors} hata,"
                    f" {time.monotonic() - round_started:.1f} s{note}",
                    flush=True,
                )
                if stats.wallets and stats.errors == stats.wallets:
                    raise RuntimeError("tüm cüzdan taramaları başarısız")
            if not wallet_mode and latest < last_block:
                print(f"UYARI: {chain} güncel blok {latest} < cursor {last_block}; tarama yok.")
            if not wallet_mode and latest > last_block:
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
                    smart_wallets = smart_wallets_for_chain(conn, chain)
                    if not smart_wallets and smart_known is not False:
                        print(
                            f"UYARI: {chain} için smart wallet yok; avimsin-filter ve"
                            " avimsin-score'u coin bazında yeniden çalıştırın."
                        )
                    smart_known = bool(smart_wallets)
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
            text = format_signal(signal, threshold, "solana" if chain == "solana" else None)
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
