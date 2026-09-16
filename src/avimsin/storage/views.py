"""Panel görünümü yardımcıları — zincir bazlı şekillendirme (saf pandas).

Streamlit'ten bağımsız: dashboard bu fonksiyonları çağırır, testler doğrudan
buradan doğrular. Zincir kolonu eklenmeden önce yazılmış kayıtlar NULL gelir
ve `bilinmiyor` olarak sınıflanır.
"""

from __future__ import annotations

import pandas as pd

UNKNOWN_CHAIN = "bilinmiyor"


def with_chain(purchases: pd.DataFrame) -> pd.DataFrame:
    """purchases'ın NULL zincirlerini `bilinmiyor` ile doldurur."""
    frame = purchases.copy()
    if "chain" in frame.columns:
        frame["chain"] = frame["chain"].fillna(UNKNOWN_CHAIN)
    return frame


def wallet_chain_map(purchases: pd.DataFrame) -> pd.Series:
    """wallet → zincir: cüzdanın en erken bloklu alımının zinciri.

    Aynı adres birden çok ağda görünebilir; en erken kayıt kazanır ki panel
    cüzdanı tek satırda göstersin.
    """
    if purchases.empty:
        return pd.Series(dtype=object)
    chain_col = with_chain(purchases)
    ordered = chain_col.sort_values("block")
    return ordered.drop_duplicates(subset="wallet").set_index("wallet")["chain"]


def chain_options(*frames: pd.DataFrame) -> list[str]:
    """Filtre seçenekleri: veride geçen zincirler, `bilinmiyor` en sonda."""
    seen: set[str] = set()
    for frame in frames:
        if "chain" in frame.columns:
            seen |= {str(v) for v in with_chain(frame)["chain"].dropna().unique()}
    return sorted(c for c in seen if c != UNKNOWN_CHAIN) + ([UNKNOWN_CHAIN] if UNKNOWN_CHAIN in seen else [])


def filter_by_chain(chain: str | None, *frames: pd.DataFrame) -> list[pd.DataFrame]:
    """`Tümü` (None) seçiliyse veriyi olduğu gibi bırakır."""
    if chain is None:
        return list(frames)
    return [
        frame[with_chain(frame)["chain"] == chain] if "chain" in frame.columns else frame
        for frame in frames
    ]