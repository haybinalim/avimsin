"""watch güvenilirliği: kalıcı cursor, teslimat hatası, pencere ve tekrar engeli."""

from __future__ import annotations

import time

import httpx
import pytest

from avimsin.alerts import watcher
from avimsin.alerts.watcher import watch_loop
from avimsin.collectors.transfers import TransferEvent
from avimsin.storage.db import connect, save_coin, save_purchase, save_verdict

C1 = "0x" + "aa" * 20
C2 = "0x" + "bb" * 20
SRC = "0x" + "99" * 20
W = ["0x" + f"{i:02x}" * 20 for i in range(1, 6)]

# robinhood 0.25 sn/blok: 100 sn = 400 blok, 1000 sn = 4000 blok.
WIN = dict(window_seconds=100, cooldown_seconds=1000)


class Chain:
    """Sahte zincir: `head` ve zaman damgasız (blok, coin, cüzdan) girişleri."""

    def __init__(self, head: int) -> None:
        self.head = head
        self.inflows: list[tuple[int, str, str]] = []
        self.ranges: list[tuple[int, int]] = []

    def block_number(self) -> int:
        return self.head

    def token_transfers(self, coin: str, start: int, end: int) -> list[TransferEvent]:
        self.ranges.append((start, end))
        return [
            TransferEvent(frm=SRC, to=w, block=b, tx=f"0xt{b}{w[-4:]}", value=5, token=c)
            for b, c, w in self.inflows
            if c == coin and start <= b <= end
        ]


class FakeTelegram:
    def __init__(self) -> None:
        self.sent: list[str] = []
        self.fail_for: set[str] = set()  # metni bu coin'i içerenler hata verir
        self.attempts = 0

    def send_message(self, chat_id=None, text="") -> dict:
        self.attempts += 1
        if any(c in text for c in self.fail_for):
            request = httpx.Request("POST", "https://api.telegram.org/botSECRET/sendMessage")
            raise httpx.HTTPStatusError(
                "boom", request=request, response=httpx.Response(500, request=request)
            )
        self.sent.append(text)
        return {"ok": True}


def make_db(chain: str = "robinhood", coins=(C1,)):
    conn = connect(":memory:")
    for c in coins:
        save_coin(conn, c, 1, chain=chain)
        for w in W:
            save_purchase(conn, c, w, 2, f"p-{c[-4:]}-{w[-4:]}")
    for w in W:
        save_verdict(conn, w, "ok", "none", "geçti")
    conn.commit()
    return conn


def run(fake: Chain, conn, tg=None, threshold=3, **kw) -> int:
    params = {**WIN, **kw}
    return watch_loop(fake, conn, once=True, threshold=threshold, telegram=tg, **params)


def test_restart_resumes_from_cursor_and_catches_downtime() -> None:
    conn = make_db()
    chain, tg = Chain(1000), FakeTelegram()
    assert run(chain, conn, tg) == 0  # ilk açılış: cursor = 1000
    # "Servis kapalıyken" üç giriş oldu, zincir 1050'ye gitti.
    chain.inflows = [(1010, C1, W[0]), (1020, C1, W[1]), (1030, C1, W[2])]
    chain.head = 1050
    chain.ranges.clear()
    assert run(chain, conn, tg) == 1  # yeniden başlatma: yeni bağlantı gibi, cursor'dan
    assert chain.ranges == [(1001, 1050)]
    assert len(tg.sent) == 1


def test_telegram_failure_does_not_kill_loop_and_retries_without_duplicates(
    monkeypatch, capsys
) -> None:
    conn = make_db(coins=(C1, C2))
    chain, tg = Chain(1000), FakeTelegram()
    run(chain, conn, tg)
    chain.inflows = [(1010 + i, c, w) for c in (C1, C2) for i, w in enumerate(W[:3])]
    chain.head = 1100
    tg.fail_for = {C2}  # kısmi başarı: C1 gider, C2 hata

    ticks = []

    def fake_sleep(_):
        ticks.append(1)
        if len(ticks) == 1:
            tg.fail_for = set()  # Telegram düzeldi
        if len(ticks) == 3:
            raise KeyboardInterrupt

    monkeypatch.setattr(time, "sleep", fake_sleep)
    with pytest.raises(KeyboardInterrupt):  # yalnız test durdurucu; döngü hatayla ölmedi
        watch_loop(chain, conn, threshold=3, telegram=tg, poll_seconds=0, **WIN)
    out = capsys.readouterr().out
    assert "HATA: Telegram gönderimi başarısız (HTTP 500)" in out
    assert "SECRET" not in out  # bot token log'a sızmaz
    coins_sent = [C1 in t for t in tg.sent]
    assert len(tg.sent) == 2 and sorted(coins_sent) == [False, True]  # her biri tam 1 kez
    assert tg.attempts == 3  # C1 ok, C2 fail, C2 retry


def test_three_wallets_in_three_separate_rounds_signal() -> None:
    conn = make_db()
    chain, tg = Chain(1000), FakeTelegram()
    run(chain, conn, tg)
    for i, head in enumerate((1100, 1200, 1300)):
        chain.inflows.append((head - 5, C1, W[i]))
        chain.head = head
        sent = run(chain, conn, tg)
        assert sent == (1 if i == 2 else 0)
    assert len(tg.sent) == 1


def test_inflow_outside_window_is_not_counted() -> None:
    conn = make_db()
    chain, tg = Chain(1000), FakeTelegram()
    run(chain, conn, tg)
    chain.inflows = [(1010, C1, W[0])]
    chain.head = 1100
    run(chain, conn, tg)
    # Pencere 400 blok: 1010 girişi 1500'de dışarıda (1500-400 > 1010).
    chain.inflows += [(1495, C1, W[1]), (1498, C1, W[2])]
    chain.head = 1500
    assert run(chain, conn, tg) == 0
    assert tg.sent == []
    assert conn.execute("SELECT COUNT(*) FROM watch_inflows").fetchone()[0] == 2  # eskisi temizlendi


def test_same_cluster_silent_new_wallet_updates_cooldown_resets() -> None:
    conn = make_db()
    chain, tg = Chain(1000), FakeTelegram()
    run(chain, conn, tg)
    chain.inflows = [(1010, C1, W[0]), (1020, C1, W[1]), (1030, C1, W[2])]
    chain.head = 1100
    assert run(chain, conn, tg) == 1
    # Aynı küme, tekrar tur: bildirim yok.
    chain.head = 1200
    assert run(chain, conn, tg) == 0
    # Yeni cüzdan: güncelleme, yeni cüzdan belirtilir.
    chain.inflows.append((1250, C1, W[3]))
    chain.head = 1300
    assert run(chain, conn, tg) == 1
    assert "güncelleme" in tg.sent[1] and f"{W[3]}</code> (blok 1250) YENİ" in tg.sent[1]
    assert "YENİ" not in tg.sent[1].split(W[0])[1].split("\n")[0]
    # Güncellemeden sonra aynı küme yine sessiz.
    chain.head = 1350
    assert run(chain, conn, tg) == 0
    # Cooldown (4000 blok) sonrası eşik yeniden aşılır: tam sinyal (güncelleme değil).
    chain.inflows += [(5400 + i, C1, W[i]) for i in range(3)]
    chain.head = 5500
    assert run(chain, conn, tg) == 1
    assert "güncelleme" not in tg.sent[2]
    assert len(tg.sent) == 3


def test_chains_do_not_mix() -> None:
    conn = connect(":memory:")
    sol = "M" * 44
    ws = ["S" * 44, "T" * 44, "U" * 44]
    save_coin(conn, C1, 1, chain="robinhood")
    save_coin(conn, sol, 1, chain="solana")
    for w in ws:
        save_purchase(conn, sol, w, 2, f"p{w[0]}")
        save_verdict(conn, w, "ok", "none", "geçti")
    for w in W[:3]:
        save_purchase(conn, C1, w, 2, f"p{w[-4:]}")
        save_verdict(conn, w, "ok", "none", "geçti")
    conn.commit()
    rh, sl, tg = Chain(1000), Chain(5000), FakeTelegram()
    run(rh, conn, tg)
    run(sl, conn, tg, chain="solana")
    # Cursor'lar zincir başına ayrı.
    cursors = dict(conn.execute("SELECT chain, last_block FROM watch_cursor"))
    assert cursors == {"robinhood": 1000, "solana": 5000}
    # Her zincirden 2 giriş: toplamda 4 ≥ 3 ama zincir başına 2 → sinyal yok.
    rh.inflows = [(1010, C1, W[0]), (1020, C1, W[1])]
    sl.inflows = [(5010, sol, ws[0]), (5020, sol, ws[1])]
    rh.head, sl.head = 1100, 5100
    assert run(rh, conn, tg) == 0
    assert run(sl, conn, tg, chain="solana") == 0
    # Solana üçüncü cüzdan → yalnız solana sinyali.
    sl.inflows.append((5150, sol, ws[2]))
    sl.head = 5200
    assert run(sl, conn, tg, chain="solana") == 1
    assert run(rh, conn, tg) == 0


def test_skipped_window_is_written_to_cursor() -> None:
    conn = make_db(chain="solana")
    chain, tg = Chain(1000), FakeTelegram()
    run(chain, conn, tg, chain="solana")
    chain.head = 100_000
    chain.ranges.clear()
    run(chain, conn, tg, chain="solana", max_slots_per_poll=500)
    assert chain.ranges == []  # taranmadı
    assert conn.execute(
        "SELECT last_block FROM watch_cursor WHERE chain = 'solana'"
    ).fetchone()[0] == 100_000
    # Kesinti sonrası yeniden başlatma da aynı kuralla atlar (Solana dahil).
    chain.head = 100_100
    run(chain, conn, tg, chain="solana", max_slots_per_poll=500)
    assert chain.ranges  # 100 aralık ≤ 500: yalnız yeni aralık taranır
    assert all(r[0] > 100_000 for r in chain.ranges)


def test_without_telegram_prints_only_and_does_not_repeat(capsys) -> None:
    conn = make_db()
    chain = Chain(1000)
    run(chain, conn)
    chain.inflows = [(1010, C1, W[0]), (1020, C1, W[1]), (1030, C1, W[2])]
    chain.head = 1100
    assert run(chain, conn) == 1
    assert "SİNYAL" in capsys.readouterr().out
    chain.head = 1200
    assert run(chain, conn) == 0


def test_window_and_cooldown_use_chain_block_interval() -> None:
    assert watcher._blocks_for(3600, "robinhood") == 14400
    assert watcher._blocks_for(3600, "solana") == 9000
