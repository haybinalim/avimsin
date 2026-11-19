"""avimsin-pipeline: kuyruktaki coin'leri scan → filter → score ile analiz eder.

Mevcut CLI'ları ayrı alt süreçlerde çağırır (mantık kopyalanmaz). Aynı anda tek
çalışma (dosya kilidi). Tur sonunda tek Telegram özeti gider (ayar yoksa stdout).

Durumlar: pending → running → done | done_warn | failed | timeout.

Kullanım::

    uv run avimsin-pipeline [--db <yol>] [--max-coins N]
"""

from __future__ import annotations

import argparse
import fcntl
import html
import json
import os
import re
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from .alerts.telegram import TelegramClient
from .alerts.watcher import smart_wallets_for_chain
from .config import settings
from .filter import SILENT_WARNING_HEAD
from .storage.db import connect as db_connect

CHAIN = "solana"
STATUS_DONE = "done"
STATUS_WARN = "done_warn"
STATUS_FAILED = "failed"
STATUS_TIMEOUT = "timeout"
# filter.py: "UYARI: {silent}/{toplam} alıcının ATA'sında pencerede hiç işlem yok; ..."
SILENT_RE = re.compile(
    re.escape(SILENT_WARNING_HEAD)
    .replace(re.escape("{silent}"), r"(\d+)")
    .replace(re.escape("{total}"), r"(\d+)")
)
TERMINATE_GRACE_SECONDS = 15

CommandBuilder = Callable[[str, str, str], list[str]]


def default_command(step: str, coin: str, db: str) -> list[str]:
    """scan/filter/score için mevcut CLI komutu (aynı yorumlayıcı, aynı DB)."""
    module = {"scan": "avimsin.scan", "filter": "avimsin.filter", "score": "avimsin.score"}[step]
    code = f"import sys; from {module} import main; sys.exit(main())"
    base = [sys.executable, "-c", code, coin, "--chain", CHAIN, "--db", db]
    return base


@dataclass
class StepResult:
    rc: int | None  # None: süre aşımı
    output: list[str]
    timed_out: bool = False


def run_step(cmd: list[str], timeout: float) -> StepResult:
    """Alt süreci çalıştırır, çıktısını canlı basar; süre aşılırsa sonlandırır."""
    env = dict(os.environ, PYTHONUNBUFFERED="1")
    proc = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, env=env,
        start_new_session=True,
    )
    lines: list[str] = []

    def pump() -> None:
        assert proc.stdout is not None
        for line in proc.stdout:
            line = line.rstrip("\n")
            lines.append(line)
            print(f"    | {line}", flush=True)

    reader = threading.Thread(target=pump, daemon=True)
    reader.start()
    timed_out = False
    try:
        proc.wait(timeout=max(timeout, 0))
    except subprocess.TimeoutExpired:
        timed_out = True
        _kill_group(proc)
    reader.join(timeout=5)
    return StepResult(None if timed_out else proc.returncode, lines, timed_out)


def _kill_group(proc: subprocess.Popen) -> None:
    import signal

    for sig, grace in ((signal.SIGTERM, TERMINATE_GRACE_SECONDS), (signal.SIGKILL, 5)):
        try:
            os.killpg(proc.pid, sig)
        except ProcessLookupError:
            return
        try:
            proc.wait(timeout=grace)
            return
        except subprocess.TimeoutExpired:
            continue


def filter_warning_ratio(output: list[str]) -> tuple[int, int] | None:
    """filter çıktısındaki "pencerede transfer yok" UYARI satırı: (sessiz, toplam) ya da None."""
    for line in output:
        m = SILENT_RE.search(line)
        if m:
            return int(m.group(1)), int(m.group(2))
    return None


@dataclass
class CoinOutcome:
    coin: str
    status: str
    reason: str
    ok_count: int | None = None


def _last_line(output: list[str]) -> str:
    for line in reversed(output):
        if line.strip():
            return line.strip()[:200]
    return "çıktı yok"


def process_coin(
    coin: str, db: str, timeout_seconds: float,
    build: CommandBuilder = default_command,
    runner: Callable[[list[str], float], StepResult] = run_step,
    clock: Callable[[], float] = time.monotonic,
) -> CoinOutcome:
    """Tek coin için scan, filter, score sırayla; coin başına toplam süre sınırı."""
    deadline = clock() + timeout_seconds
    filter_output: list[str] = []
    for step in ("scan", "filter", "score"):
        remaining = deadline - clock()
        if remaining <= 0:
            return CoinOutcome(coin, STATUS_TIMEOUT, f"{step} başlamadan süre doldu")
        print(f"  [{coin}] {step} başladı", flush=True)
        result = runner(build(step, coin, db), remaining)
        if result.timed_out:
            return CoinOutcome(coin, STATUS_TIMEOUT, f"{step} {timeout_seconds:g} sn sınırını aştı")
        if result.rc != 0:
            return CoinOutcome(
                coin, STATUS_FAILED, f"{step} rc={result.rc}: {_last_line(result.output)}"
            )
        if step == "filter":
            filter_output = result.output
    ok_count = _ok_count(db, coin)
    warn = filter_warning_ratio(filter_output)
    if warn and warn[0] * 2 > warn[1]:
        return CoinOutcome(
            coin, STATUS_WARN,
            f"ok={ok_count}; UYARI {warn[0]}/{warn[1]} alıcıda pencerede transfer yok (eski coin?)",
            ok_count,
        )
    return CoinOutcome(coin, STATUS_DONE, f"ok={ok_count}", ok_count)


def _ok_count(db: str, coin: str) -> int:
    conn = db_connect(db)
    try:
        return conn.execute(
            "SELECT COUNT(*) FROM coin_verdicts WHERE coin = ? AND verdict = 'ok'", (coin,)
        ).fetchone()[0]
    finally:
        conn.close()


def set_status(conn, coin: str, status: str, reason: str | None = None) -> None:
    with conn:
        conn.execute(
            "UPDATE discovery_queue SET status = ?, reason = COALESCE(?, reason)"
            " WHERE coin = ? AND chain = ?",
            (status, reason, coin, CHAIN),
        )


def pick_queue(conn, limit: int) -> list[str]:
    """pending coin'ler, 24s hacmi en yüksek önce."""
    rows = conn.execute(
        "SELECT coin, metrics_json FROM discovery_queue WHERE status = 'pending' AND chain = ?",
        (CHAIN,),
    ).fetchall()

    def volume(row) -> float:
        try:
            return float(json.loads(row[1] or "{}").get("volume_24h_usd") or 0)
        except (ValueError, TypeError):
            return 0.0

    rows.sort(key=volume, reverse=True)
    return [r[0] for r in rows[:limit]]


def build_summary(outcomes: list[CoinOutcome], before: int, after: int) -> str:
    """Telegram (HTML) özet metni."""
    lines = ["<b>Avımsın günlük tarama</b>", f"{len(outcomes)} coin işlendi."]
    if outcomes and all(o.status in (STATUS_FAILED, STATUS_TIMEOUT) for o in outcomes):
        lines.append("<b>UYARI: TÜM COİNLER BAŞARISIZ OLDU.</b>")
    for o in outcomes:
        url = f"https://dexscreener.com/solana/{o.coin}"
        ok = f"ok={o.ok_count}" if o.ok_count is not None else "ok=-"
        lines.append(
            f"- {o.status} {ok}: <a href=\"{url}\">{html.escape(o.coin[:8])}…</a> "
            f"{html.escape(o.reason)}"
        )
    lines.append(f"Smart wallet toplamı: {before} → {after}")
    return "\n".join(lines)


def send_summary(text: str, telegram: TelegramClient | None = None) -> None:
    """Telegram ayarlıysa gönderir; yoksa (ya da hata olursa) stdout'a basar."""
    if not (settings.telegram_bot_token and settings.telegram_chat_id):
        print("Telegram ayarı yok; özet:\n" + text)
        return
    client = telegram or TelegramClient()
    try:
        client.send_message(text=text)
    except Exception as exc:  # özet gitmediyse sessiz kalma: stdout + stderr
        print(f"UYARI: Telegram özeti gönderilemedi ({type(exc).__name__}: {exc}); özet:\n" + text,
              file=sys.stderr)


def run_pipeline(
    conn, db: str, *, max_coins: int, timeout_seconds: float,
    build: CommandBuilder = default_command,
    runner: Callable[[list[str], float], StepResult] = run_step,
    telegram: TelegramClient | None = None,
) -> list[CoinOutcome]:
    """Kuyruktan coin alıp işler, özet gönderir.

    UYARI: baştaki ``running`` → ``failed`` temizliği yalnız dosya kilidi (main) altında
    güvenlidir; kilitsiz ikinci bir pipeline çalışırken çağrılırsa canlı işi ``failed`` yapar.
    """
    # Önceki çalışma yarıda kaldıysa (çökme, makine kapanması) 'running' kalmıştır.
    with conn:
        conn.execute(
            "UPDATE discovery_queue SET status = 'failed', reason = 'önceki çalışma yarıda kaldı'"
            " WHERE status = 'running'"
        )
    before = len(smart_wallets_for_chain(conn, CHAIN))
    coins = pick_queue(conn, max_coins)
    print(f"Kuyruktan {len(coins)} coin alındı (smart wallet: {before}).")
    outcomes: list[CoinOutcome] = []
    for coin in coins:
        set_status(conn, coin, "running")
        try:
            outcome = process_coin(coin, db, timeout_seconds, build, runner)
        except Exception as exc:  # bir coin'in beklenmedik hatası sıradakini engellemesin
            outcome = CoinOutcome(coin, STATUS_FAILED, f"beklenmedik hata: {type(exc).__name__}: {exc}")
        set_status(conn, coin, outcome.status, outcome.reason)
        print(f"{coin}: {outcome.status} — {outcome.reason}", flush=True)
        outcomes.append(outcome)
    after = len(smart_wallets_for_chain(conn, CHAIN))
    summary = build_summary(outcomes, before, after)
    if not outcomes:
        summary += "\nKuyruk boş; işlenecek coin yok."
    send_summary(summary, telegram)
    return outcomes


def main(argv: list[str] | None = None) -> None:
    """CLI giriş noktası."""
    parser = argparse.ArgumentParser(
        description="Kuyruktaki coin'leri scan → filter → score ile analiz eder."
    )
    parser.add_argument("--db", default="data/avimsin.sqlite", help="SQLite dosya yolu")
    parser.add_argument("--max-coins", type=int, default=None, help="Tur başına en çok coin")
    parser.add_argument("--lock-file", default=None, help="Kilit dosyası (varsayılan: <db>.pipeline.lock)")
    args = parser.parse_args(argv)

    lock_path = Path(args.lock_file or f"{args.db}.pipeline.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock = open(lock_path, "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print(f"Başka bir pipeline çalışıyor (kilit: {lock_path}); çıkılıyor.")
        lock.close()
        sys.exit(0)

    conn = db_connect(args.db)
    try:
        outcomes = run_pipeline(
            conn, args.db,
            max_coins=args.max_coins if args.max_coins is not None else settings.discovery_max_coins_per_run,
            timeout_seconds=settings.pipeline_coin_timeout_seconds,
        )
    finally:
        conn.close()
        lock.close()
    if outcomes and all(o.status in (STATUS_FAILED, STATUS_TIMEOUT) for o in outcomes):
        sys.exit(1)
