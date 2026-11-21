"""Sır sızıntısı: ham hata mesajı (URL: bot token, RPC anahtarı) log/özet/kuyruğa gitmemeli."""

from __future__ import annotations

import dataclasses
import sys

import httpx

from avimsin import pipeline
from avimsin.pipeline import process_coin, run_pipeline
from avimsin.storage.db import connect

A = "A" * 44
TOKEN = "123456:SIR-BOT-TOKEN"


def _status_error(url: str, code: int) -> httpx.HTTPStatusError:
    req = httpx.Request("POST", url)
    return httpx.HTTPStatusError(
        f"Client error '{code}' for url '{url}'", request=req,
        response=httpx.Response(code, request=req),
    )


def _with_telegram(monkeypatch):
    s = dataclasses.replace(pipeline.settings, telegram_bot_token="t", telegram_chat_id="1")
    monkeypatch.setattr(pipeline, "settings", s)


def _queue(conn, coin):
    conn.execute(
        "INSERT INTO discovery_queue (coin, chain, source, found_at, status, reason, metrics_json)"
        " VALUES (?, 'solana', 'test', 'now', 'pending', 'aday', '{}')",
        (coin,),
    )
    conn.commit()


def test_send_summary_failure_does_not_leak_token(monkeypatch, capsys):
    _with_telegram(monkeypatch)

    class Failing:
        def send_message(self, chat_id=None, text=""):
            raise _status_error(f"https://api.telegram.org/bot{TOKEN}/sendMessage", 401)

    pipeline.send_summary("özet", Failing())
    cap = capsys.readouterr()
    assert "SIR-BOT-TOKEN" not in cap.out + cap.err
    assert "HTTP 401" in cap.err


def test_unexpected_error_does_not_leak_api_key(tmp_path, monkeypatch):
    _with_telegram(monkeypatch)
    db = str(tmp_path / "t.sqlite")
    conn = connect(db)
    _queue(conn, A)

    def boom(*a, **k):
        raise _status_error("https://rpc.example/v1?api-key=SIR", 403)

    class Capture:
        sent: list[str] = []

        def send_message(self, chat_id=None, text=""):
            self.sent.append(text)

    tg = Capture()
    monkeypatch.setattr(pipeline, "process_coin", boom)
    run_pipeline(conn, db, max_coins=3, timeout_seconds=30, telegram=tg)
    reason = conn.execute("SELECT reason FROM discovery_queue WHERE coin = ?", (A,)).fetchone()[0]
    assert "SIR" not in reason and "HTTP 403" in reason
    assert len(tg.sent) == 1 and "SIR" not in tg.sent[0]


def test_step_output_urls_are_redacted(tmp_path):
    leak = "HTTPStatusError: 403 for url 'https://rpc.example/v1?api-key=SIR'"
    code = f"import sys; print({leak!r}, file=sys.stderr); sys.exit(1)"

    def build(step, coin, db):
        return [sys.executable, "-c", code if step == "scan" else "pass"]

    out = process_coin(A, str(tmp_path / "t.sqlite"), 30, build)
    assert out.status == "failed" and "SIR" not in out.reason and "<url>" in out.reason
