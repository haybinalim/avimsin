"""Kalıcı analiz penceresi: avimsin-filter ve avimsin-score aynı (from, to) aralığını kullanır.

Neden: `--to-block` verilmeyince "en güncel blok" kullanmak her çalıştırmada
pencereyi kaydırıyordu; sonuçlar karşılaştırılamıyor, Solana'da ikinci çalıştırma
yüz binlerce ek slot isteyebiliyordu. Pencere `analysis_windows` tablosunda tutulur.

Kurallar:
- Hedef uzunluk ``ANALYSIS_WINDOW_SECONDS`` (varsayılan 24 saat); zincirin
  blok/slot süresiyle (BLOCK_INTERVAL_SECONDS) bloğa çevrilir. Hedef to = from + pencere.
- Hedef latest'i aşarsa latest'te kesilir ve kayıt complete=0 olur (eksik); sonraki
  çalıştırmada hedef yeniden hesaplanır, latest yetiştiyse genişler ve complete=1 olur.
- complete=1 pencere bir daha kaymaz: latest ne olursa olsun kayıtlı to_block kullanılır.
- Açık --from-block/--to-block kullanılır, kayda yazılır; açık --to-block > latest hatadır.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Callable

from .config import settings
from .filters.chains import blocks_for_seconds
from .storage.db import load_window, normalize_address, save_window


@dataclass(frozen=True)
class Window:
    from_block: int
    to_block: int
    complete: bool
    target_blocks: int  # hedef pencere uzunluğu (blok/slot)
    notes: tuple[str, ...] = ()  # çıktıya yazılacak satırlar (pencere başlığı, uyarı, güncelleme)


def _fmt(window: tuple[int, int, bool] | None) -> str:
    if window is None:
        return "yok"
    return f"{window[0]}–{window[1]} ({'tam' if window[2] else 'eksik'})"


def resolve_window(
    conn: sqlite3.Connection,
    coin: str,
    chain: str,
    latest: Callable[[], int],
    from_arg: int | None = None,
    to_arg: int | None = None,
    window_seconds: int | None = None,
) -> Window:
    """Pencereyi çözer ve DB'ye yazar (çağıran işlemi commit eder). Hata: SystemExit."""
    coin = normalize_address(coin)
    stored = load_window(conn, coin)
    target = blocks_for_seconds(
        settings.analysis_window_seconds if window_seconds is None else window_seconds, chain
    )

    if from_arg is not None:
        from_block = from_arg
    elif stored is not None:
        from_block = stored[0]
    else:
        row = conn.execute("SELECT first_block FROM coins WHERE address = ?", (coin,)).fetchone()
        if row is None:
            raise SystemExit(f"{coin} veritabanında yok; önce avimsin-scan çalıştırın.")
        from_block = row[0]

    notes: list[str] = []
    if to_arg is None and stored is not None and stored[2] and stored[0] == from_block:
        # Tamamlanmış pencere kaymaz: latest'e bakılmaz.
        to_block, complete = stored[1], True
    else:
        latest_block = latest()
        if to_arg is not None:
            if to_arg > latest_block:
                raise SystemExit(
                    f"--to-block {to_arg} en güncel bloktan ({latest_block}) büyük; "
                    "gelecek blok taranamaz."
                )
            to_block, complete = to_arg, True
        else:
            goal = from_block + target
            if goal <= latest_block:
                to_block, complete = goal, True
            else:
                to_block, complete = latest_block, False
        if to_block < from_block:
            raise SystemExit(
                f"Pencere geçersiz: başlangıç {from_block}, bitiş {to_block} "
                f"(en güncel blok {latest_block})."
            )

    new = (from_block, to_block, complete)
    explicit = from_arg is not None or to_arg is not None
    if new != stored:
        save_window(conn, coin, from_block, to_block, complete)
    notes.append(f"Analiz penceresi: {from_block}–{to_block} ({'tam' if complete else 'eksik'})")
    if explicit and new != stored:
        notes.append(f"Pencere güncellendi: {_fmt(stored)} → {_fmt(new)}")
    if not complete:
        notes.append(
            f"UYARI: pencere eksik ({to_block - from_block} / {target} blok); "
            "sonraki çalıştırmada tamamlanacak"
        )
    return Window(from_block, to_block, complete, target, tuple(notes))
