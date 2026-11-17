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

# --- coin bazlı görünümler (coin_verdicts / coin_scores) ---

COIN_VERDICT_COLUMNS = ["coin", "wallet", "verdict", "rule", "reason"]
COIN_SCORE_COLUMNS = ["coin", "wallet", "winrate", "net_pnl", "trades", "frequency", "score"]
LEGACY_RESULTS_NOTE = (
    "Coin bazlı karar/skor kaydı yok. Eski global kayıtların coin bilgisi bulunmadığı için "
    "gösterilmez; avimsin-filter ve avimsin-score'u coin bazında yeniden çalıştırın."
)


def has_coin_results(verdicts: pd.DataFrame, scores: pd.DataFrame) -> bool:
    """Yeni coin bazlı tablolardan en az birinde kayıt var mı?"""
    return not (verdicts.empty and scores.empty)


def coin_options(coins: pd.DataFrame) -> list[str]:
    """Coin seçici seçenekleri: (zincir filtresi uygulanmış) coin adresleri, sıralı."""
    if coins.empty or "address" not in coins.columns:
        return []
    return sorted(str(a) for a in coins["address"].dropna().unique())


def coin_view(
    coin: str, purchases: pd.DataFrame, verdicts: pd.DataFrame, scores: pd.DataFrame
) -> pd.DataFrame:
    """Tek coin: o coin'in alıcıları, o coin'in kararı ve skoruyla (skora göre azalan).

    Kararı olmayan alıcı `verdict=None` (filtrelenmedi) gelir; başka coin'in
    karar/skoru asla karışmaz.
    """
    buyers = purchases.loc[purchases["coin"] == coin, ["wallet"]].drop_duplicates()
    v = verdicts.loc[verdicts["coin"] == coin, ["wallet", "verdict", "rule", "reason"]]
    s = scores.loc[
        scores["coin"] == coin, ["wallet", "winrate", "net_pnl", "trades", "frequency", "score"]
    ]
    out = buyers.merge(v, on="wallet", how="left").merge(s, on="wallet", how="left")
    return out.sort_values("score", ascending=False, na_position="last", kind="stable").reset_index(
        drop=True
    )


def all_coins_view(
    purchases: pd.DataFrame, verdicts: pd.DataFrame, scores: pd.DataFrame
) -> pd.DataFrame:
    """Tümü: cüzdan başına en iyi skor, kaç coin'de ok (`ok_coins`), bot sayısı ve işareti.

    `best_coin` en iyi skorun coin'idir. `is_bot` herhangi bir coin'de bot kararı
    varsa True'dur (watcher de bu cüzdanı dışlar).
    """
    columns = ["wallet", "best_score", "best_coin", "ok_coins", "dump_coins", "bot_coins", "is_bot"]
    wallets = purchases[["wallet"]].drop_duplicates()
    if wallets.empty:
        return pd.DataFrame(columns=columns)
    counts = (
        verdicts.assign(one=1)
        .pivot_table(index="wallet", columns="verdict", values="one", aggfunc="sum", fill_value=0)
        if not verdicts.empty
        else pd.DataFrame()
    )
    out = wallets.set_index("wallet")
    for verdict, name in (("ok", "ok_coins"), ("dump", "dump_coins"), ("bot", "bot_coins")):
        col = counts[verdict] if verdict in counts.columns else pd.Series(dtype=int)
        out[name] = col.reindex(out.index).fillna(0).astype(int)
    out["is_bot"] = out["bot_coins"] > 0
    if scores.empty:
        out["best_score"] = float("nan")
        out["best_coin"] = None
    else:
        best = scores.sort_values("score", ascending=False, kind="stable").drop_duplicates("wallet")
        best = best.set_index("wallet")
        out["best_score"] = best["score"].reindex(out.index)
        out["best_coin"] = best["coin"].reindex(out.index)
    out = out.reset_index()[columns]
    return out.sort_values("best_score", ascending=False, na_position="last", kind="stable").reset_index(
        drop=True
    )
