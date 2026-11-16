"""watch RPC hata dayanıklılığı: döngü ölmez, hepsi-ya-hiç, sızıntı yok, once çıkış kodu."""

from __future__ import annotations

import pytest

from avimsin.alerts import watcher
from avimsin.alerts.watcher import watch_loop
from avimsin.collectors.transfers import TransferEvent
from avimsin.storage.db import connect

from .test_watch_reliability import WIN, C1, C2, W, Chain, FakeTelegram, make_db, run


class FlakyChain(Chain):
    """`fail_coins` içindeki coin ya da `fail_head` için URL'li hata atar."""

    def __init__(self, head: int) -> None:
        super().__init__(head)
        self.fail_coins: set[str] = set()
        self.fail_head = False

    def block_number(self) -> int:
        if self.fail_head:
            raise RuntimeError("rpc eth_blockNumber failed: https://x/?api-key=SECRET")
        return self.head

    def token_transfers(self, coin: str, start: int, end: int) -> list[TransferEvent]:
        if coin in self.fail_coins:
            raise RuntimeError("rpc eth_getLogs failed: https://x/?api-key=SECRET")
        return super().token_transfers(coin, start, end)


class _Stop(Exception):
    pass


def _daemon_rounds(fake, conn, tg, rounds: int, monkeypatch, between=None) -> None:
    """Daemon modunu `rounds` tur çalıştırır (sleep sayaçla durdurur)."""
    count = {"n": 0}

    def fake_sleep(_s):
        count["n"] += 1
        if between:
            between(count["n"])
        if count["n"] >= rounds:
            raise _Stop

    monkeypatch.setattr(watcher.time, "sleep", fake_sleep)
    with pytest.raises(_Stop):
        watch_loop(fake, conn, threshold=3, telegram=tg, poll_seconds=0, **WIN)


def _cursor(conn) -> int:
    return conn.execute("SELECT last_block FROM watch_cursor").fetchone()[0]


def test_transfer_error_keeps_loop_alive_and_retries_same_range(monkeypatch, capsys) -> None:
    conn = make_db()
    chain, tg = FlakyChain(1000), FakeTelegram()
    run(chain, conn, tg)  # cursor = 1000
    chain.head = 1010
    chain.inflows = [(1005, C1, w) for w in W[:3]]
    chain.fail_coins = {C1}

    def between(n):
        if n == 1:
            assert _cursor(conn) == 1000  # hata turunda ilerlemedi
            chain.fail_coins = set()

    _daemon_rounds(chain, conn, tg, 2, monkeypatch, between)
    assert _cursor(conn) == 1010
    assert chain.ranges[-1] == (1001, 1010)  # aynı aralık yeniden tarandı
    assert len(tg.sent) == 1
    assert "HATA: robinhood RPC" in capsys.readouterr().out


def test_partial_failure_writes_no_inflows(monkeypatch) -> None:
    conn = make_db(coins=(C1, C2))
    chain, tg = FlakyChain(1000), FakeTelegram()
    run(chain, conn, tg)
    chain.head = 1010
    chain.inflows = [(1005, C1, w) for w in W[:3]]
    chain.fail_coins = {C2}  # C1 başarılı, C2 hatalı
    checked = []

    def between(_n):
        n = conn.execute("SELECT COUNT(*) FROM watch_inflows").fetchone()[0]
        assert n == 0 and _cursor(conn) == 1000
        checked.append(True)

    _daemon_rounds(chain, conn, tg, 1, monkeypatch, between)
    assert checked and tg.sent == []


def test_block_number_error_still_sends_pending_signals(monkeypatch) -> None:
    conn = make_db()
    chain, tg = FlakyChain(1000), FakeTelegram()
    run(chain, conn, tg)
    chain.head = 1010
    chain.inflows = [(1005, C1, w) for w in W[:3]]
    tg.fail_for = {C1}
    run(chain, conn, tg)  # girişler yazıldı, gönderim başarısız
    assert tg.sent == []
    tg.fail_for = set()
    chain.fail_head = True
    _daemon_rounds(chain, conn, tg, 1, monkeypatch)
    assert len(tg.sent) == 1  # block_number hatasında da bekleyen sinyal gitti
    assert _cursor(conn) == 1010


def test_error_log_does_not_leak_url_or_key(monkeypatch, capsys) -> None:
    conn = make_db()
    chain, tg = FlakyChain(1000), FakeTelegram()
    run(chain, conn, tg)
    chain.head = 1010
    chain.fail_coins = {C1}
    _daemon_rounds(chain, conn, tg, 1, monkeypatch)
    chain.fail_head = True
    _daemon_rounds(chain, conn, tg, 1, monkeypatch)
    out = capsys.readouterr().out
    assert "HATA: robinhood RPC" in out
    assert "SECRET" not in out and "api-key" not in out and "https://x" not in out


def test_once_mode_raises_on_rpc_error() -> None:
    conn = make_db()
    chain, tg = FlakyChain(1000), FakeTelegram()
    run(chain, conn, tg)
    chain.head = 1010
    chain.fail_coins = {C1}
    with pytest.raises(watcher.WatchRpcError) as ei:
        run(chain, conn, tg)
    assert "SECRET" not in str(ei.value) and ei.value.__cause__ is None
    assert _cursor(conn) == 1000


def test_cli_once_rpc_error_exits_nonzero(monkeypatch) -> None:
    from avimsin import watch as watch_cli

    def boom(*a, **k):
        raise watcher.WatchRpcError("robinhood RPC turu başarısız")

    class Ctx:
        def __enter__(self):
            return object()

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(watch_cli, "watch_loop", boom)
    monkeypatch.setattr(watch_cli, "open_chain", lambda c: Ctx())
    monkeypatch.setattr(watch_cli, "TelegramClient", lambda: object())
    monkeypatch.setattr(watch_cli, "db_connect", lambda p: connect(":memory:"))
    monkeypatch.setattr("sys.argv", ["avimsin-watch", "--once"])
    with pytest.raises(SystemExit) as ei:
        watch_cli.main()
    assert ei.value.code == 1
