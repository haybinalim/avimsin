"""avimsin-pipeline: sıradaki coin'e geçiş, timeout, kilit, done_warn, Telegram özeti.

scan/filter/score yerine gerçek alt süreçler olarak sahte komutlar çalışır
(python -c); ağ ve zincir yok.
"""

from __future__ import annotations

import dataclasses
import fcntl
import json
import pathlib
import sqlite3
import sys
import threading
import time

import pytest

from avimsin import pipeline
from avimsin.alerts.watcher import smart_wallets_for_chain
from avimsin.pipeline import (
    build_summary,
    pick_queue,
    process_coin,
    run_pipeline,
)
from avimsin.storage.db import connect, save_coin, save_purchase, save_verdict

A = "A" * 44
B = "B" * 44
C = "C" * 44
WARN_LINE = "UYARI: 8/10 alıcının ATA'sında pencerede hiç işlem yok; sonuçlara güvenmeyin."

# score adımı: coin'i + 2 temiz cüzdanı DB'ye yazar (gerçek score'un yan etkisi taklidi).
WRITE_OK = (
    "import sys; from avimsin.storage.db import connect, save_coin, save_verdict;"
    "c = connect(sys.argv[2]);"
    "save_coin(c, sys.argv[1], 1, chain='solana');"
    "[save_verdict(c, sys.argv[1], sys.argv[1][:3] + str(i), 'ok', '-', 'x') for i in range(2)];"
    "c.commit()"
)


def make_builder(overrides: dict[tuple[str, str], str]):
    def build(step: str, coin: str, db: str) -> list[str]:
        code = overrides.get((coin, step), WRITE_OK if step == "score" else "pass")
        return [sys.executable, "-c", code, coin, db]

    return build


def queue(conn, coin: str, volume: float, status: str = "pending") -> None:
    conn.execute(
        "INSERT INTO discovery_queue (coin, chain, source, found_at, status, reason, metrics_json)"
        " VALUES (?, 'solana', 'test', 'now', ?, 'aday', ?)",
        (coin, status, json.dumps({"volume_24h_usd": volume})),
    )
    conn.commit()


def status_of(conn, coin: str) -> tuple[str, str]:
    return conn.execute(
        "SELECT status, reason FROM discovery_queue WHERE coin = ?", (coin,)
    ).fetchone()


@pytest.fixture
def db(tmp_path):
    return str(tmp_path / "avimsin.sqlite")


@pytest.fixture
def captured_summary(monkeypatch):
    sent: list[str] = []
    monkeypatch.setattr(pipeline, "send_summary", lambda text, telegram=None: sent.append(text))
    return sent


def test_queue_picks_highest_volume_first_and_only_pending(db):
    conn = connect(db)
    queue(conn, "low", 10)
    queue(conn, "high", 900)
    queue(conn, "mid", 500)
    queue(conn, "old", 9999, status="done")
    assert pick_queue(conn, 2) == ["high", "mid"]


def test_done_with_ok_count_and_smart_wallets_grow(db, captured_summary):
    conn = connect(db)
    queue(conn, A, 100)
    outcomes = run_pipeline(
        conn, db, max_coins=3, timeout_seconds=30, build=make_builder({})
    )
    assert [o.status for o in outcomes] == ["done"]
    assert status_of(conn, A) == ("done", "ok=2")
    assert len(smart_wallets_for_chain(conn, "solana")) == 2
    assert "Smart wallet toplamı: 0 → 2" in captured_summary[0]
    assert f"https://dexscreener.com/solana/{A}" in captured_summary[0]
    assert "ok=2" in captured_summary[0]


def test_failed_coin_does_not_stop_next(db, captured_summary):
    conn = connect(db)
    queue(conn, A, 900)
    queue(conn, B, 100)
    build = make_builder({(A, "filter"): "print('patladi: neden'); import sys; sys.exit(3)"})
    outcomes = run_pipeline(conn, db, max_coins=3, timeout_seconds=30, build=build)
    assert [o.status for o in outcomes] == ["failed", "done"]
    status, reason = status_of(conn, A)
    assert status == "failed" and "filter rc=3" in reason and "patladi: neden" in reason
    assert status_of(conn, B)[0] == "done"


def test_timeout_kills_process_and_moves_on(db, captured_summary):
    conn = connect(db)
    queue(conn, A, 900)
    queue(conn, B, 100)
    build = make_builder({(A, "scan"): "import time; time.sleep(60)"})
    outcomes = run_pipeline(conn, db, max_coins=3, timeout_seconds=1, build=build)
    assert [o.status for o in outcomes] == ["timeout", "done"]
    assert status_of(conn, A)[0] == "timeout"
    assert status_of(conn, B)[0] == "done"


def test_failed_step_stops_following_steps(db):
    marker = "import pathlib, sys; pathlib.Path(sys.argv[2] + '.ran').write_text('x')"
    build = make_builder({(A, "scan"): "import sys; sys.exit(1)", (A, "filter"): marker})
    out = process_coin(A, db, 30, build=build)
    assert out.status == "failed" and "scan rc=1" in out.reason
    assert not pathlib.Path(db + ".ran").exists()


def test_done_warn_when_most_buyers_silent(db):
    conn = connect(db)
    queue(conn, A, 100)
    build = make_builder({(A, "filter"): f"print({WARN_LINE!r})"})
    (outcome,) = run_pipeline(conn, db, max_coins=1, timeout_seconds=30, build=build)
    assert outcome.status == "done_warn"
    status, reason = status_of(conn, A)
    assert status == "done_warn" and "8/10" in reason


def test_no_warn_when_half_or_less_silent(db):
    line = "UYARI: 5/10 alıcının ATA'sında pencerede hiç işlem yok; x"
    out = process_coin(A, db, 30, build=make_builder({(A, "filter"): f"print({line!r})"}))
    assert out.status == "done"


def test_leftover_running_marked_failed(db, captured_summary):
    conn = connect(db)
    queue(conn, A, 100, status="running")
    run_pipeline(conn, db, max_coins=3, timeout_seconds=30, build=make_builder({}))
    assert status_of(conn, A) == ("failed", "önceki çalışma yarıda kaldı")


# --- Telegram özeti ---

class FakeTelegram:
    def __init__(self, fail: bool = False):
        self.sent: list[str] = []
        self.fail = fail

    def send_message(self, chat_id=None, text=""):
        if self.fail:
            raise RuntimeError("telegram yok")
        self.sent.append(text)
        return {"ok": True}


def with_telegram(monkeypatch):
    s = dataclasses.replace(pipeline.settings, telegram_bot_token="t", telegram_chat_id="1")
    monkeypatch.setattr(pipeline, "settings", s)


def test_summary_sent_once_via_telegram(db, monkeypatch):
    with_telegram(monkeypatch)
    conn = connect(db)
    queue(conn, A, 900)
    queue(conn, B, 100)
    tg = FakeTelegram()
    run_pipeline(conn, db, max_coins=3, timeout_seconds=30, build=make_builder({}), telegram=tg)
    assert len(tg.sent) == 1
    text = tg.sent[0]
    assert "2 coin işlendi" in text and "0 → 4" in text
    assert f"dexscreener.com/solana/{A}" in text and f"dexscreener.com/solana/{B}" in text


def test_summary_printed_when_no_telegram_settings(db, monkeypatch, capsys):
    s = dataclasses.replace(pipeline.settings, telegram_bot_token="", telegram_chat_id="")
    monkeypatch.setattr(pipeline, "settings", s)
    conn = connect(db)
    queue(conn, A, 100)
    tg = FakeTelegram()
    run_pipeline(conn, db, max_coins=3, timeout_seconds=30, build=make_builder({}), telegram=tg)
    assert tg.sent == []
    assert "Avımsın günlük tarama" in capsys.readouterr().out


def test_telegram_failure_is_visible(db, monkeypatch, capsys):
    with_telegram(monkeypatch)
    conn = connect(db)
    queue(conn, A, 100)
    run_pipeline(
        conn, db, max_coins=3, timeout_seconds=30, build=make_builder({}),
        telegram=FakeTelegram(fail=True),
    )
    assert "Telegram özeti gönderilemedi" in capsys.readouterr().err


def test_all_failed_is_explicit_in_summary():
    outs = [
        pipeline.CoinOutcome(A, "failed", "scan rc=1: x"),
        pipeline.CoinOutcome(B, "timeout", "scan süre"),
    ]
    text = build_summary(outs, 5, 5)
    assert "TÜM COİNLER BAŞARISIZ" in text and "5 → 5" in text
    ok_text = build_summary([pipeline.CoinOutcome(A, "done", "ok=1", 1), outs[0]], 5, 6)
    assert "TÜM COİNLER" not in ok_text


def test_empty_queue_still_reports(db, captured_summary):
    conn = connect(db)
    assert run_pipeline(conn, db, max_coins=3, timeout_seconds=30, build=make_builder({})) == []
    assert "Kuyruk boş" in captured_summary[0]


# --- kilit ---

def test_lock_held_means_exit_0_and_nothing_runs(db, capsys, monkeypatch):
    conn = connect(db)
    queue(conn, A, 100)
    conn.close()
    called = []
    monkeypatch.setattr(pipeline, "run_pipeline", lambda *a, **k: called.append(1))
    holder = open(db + ".pipeline.lock", "w")
    fcntl.flock(holder, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        with pytest.raises(SystemExit) as exc:
            pipeline.main(["--db", db])
    finally:
        holder.close()
    assert exc.value.code == 0
    assert called == []
    assert "Başka bir pipeline çalışıyor" in capsys.readouterr().out


def test_lock_free_runs_and_releases(db, monkeypatch):
    monkeypatch.setattr(pipeline, "run_pipeline", lambda *a, **k: [])
    pipeline.main(["--db", db])  # kilit alınır, bırakılır
    holder = open(db + ".pipeline.lock", "w")
    fcntl.flock(holder, fcntl.LOCK_EX | fcntl.LOCK_NB)  # tekrar alınabilmeli
    holder.close()


def test_main_exit_1_when_all_fail(db, monkeypatch):
    monkeypatch.setattr(
        pipeline, "run_pipeline", lambda *a, **k: [pipeline.CoinOutcome(A, "failed", "x")]
    )
    with pytest.raises(SystemExit) as exc:
        pipeline.main(["--db", db])
    assert exc.value.code == 1


# --- SQLite eşzamanlılığı ---

def test_wal_and_busy_timeout_enabled(db):
    conn = connect(db)
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert conn.execute("PRAGMA busy_timeout").fetchone()[0] >= 30000


def test_concurrent_writers_do_not_lock(db):
    """İki bağlantı (watch + pipeline) aynı anda yazar; kilit hatası olmamalı."""
    connect(db).close()
    errors: list[Exception] = []

    def writer(tag):
        conn = connect(db)  # bağlantı kendi iş parçacığında
        try:
            for i in range(200):
                with conn:
                    save_purchase(conn, f"coin{tag}", f"w{tag}{i}", i, f"tx{tag}{i}")
        except sqlite3.OperationalError as exc:
            errors.append(exc)
        finally:
            conn.close()

    threads = [threading.Thread(target=writer, args=(t,)) for t in ("a", "b")]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    assert connect(db).execute("SELECT COUNT(*) FROM purchases").fetchone()[0] == 400


def test_writer_waits_for_held_lock_instead_of_failing(db):
    """Yazma kilidi tutulurken ikinci yazar busy_timeout kadar bekler, 'locked' hatası vermez."""
    holder = connect(db)
    holder.execute("BEGIN IMMEDIATE")
    result: list[object] = []

    def second():
        conn = connect(db)
        try:
            with conn:
                save_purchase(conn, "c", "w", 1, "t")
            result.append("ok")
        except sqlite3.OperationalError as exc:
            result.append(exc)
        finally:
            conn.close()

    t = threading.Thread(target=second)
    t.start()
    time.sleep(0.5)
    assert result == []  # hâlâ bekliyor
    holder.commit()
    t.join(timeout=10)
    assert result == ["ok"]


def test_reader_not_blocked_by_open_write_transaction(db):
    writer, reader = connect(db), connect(db)
    save_coin(writer, "c0", 1, chain="solana")
    writer.commit()
    writer.execute("BEGIN IMMEDIATE")
    save_verdict(writer, "c0", "w", "ok", "-", "x")  # açık, commitsiz yazma
    assert reader.execute("SELECT COUNT(*) FROM coins").fetchone()[0] == 1  # WAL: okuma bloklanmaz
    writer.rollback()
