"""Avımsın dashboard — smart wallet listesini tarayıcıda gösterir.

Çalıştırma::

    uv run streamlit run dashboard/app.py
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

import pandas as pd
import streamlit as st

from avimsin.storage.views import (
    LEGACY_RESULTS_NOTE,
    UNKNOWN_CHAIN,
    all_coins_view,
    chain_options,
    coin_options,
    coin_view,
    filter_by_chain,
    has_coin_results,
    wallet_chain_map,
    with_chain,
)

DB_PATH = Path(os.environ.get("AVIMSIN_DB", "data/avimsin.sqlite"))

VERDICT_LABEL = {
    "ok": "✅ Temiz",
    "bot": "🤖 Bot",
    "dump": "📉 Kaçak satış",
    None: "⏳ Filtrelenmedi",
}

st.set_page_config(page_title="Avımsın", page_icon="🏹", layout="wide")


Frames = tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.DataFrame]


@st.cache_data(ttl=30)
def load_data(db_path: str | None = None) -> Frames:
    """coins, wallets, purchases, coin_verdicts ve coin_scores tablolarını zincir bilgisiyle okur."""
    path = Path(db_path) if db_path else DB_PATH
    if not path.exists():
        return tuple(pd.DataFrame() for _ in range(5))  # type: ignore[return-value]
    conn = sqlite3.connect(path)
    try:
        coins = pd.read_sql("SELECT * FROM coins", conn)
        wallets = pd.read_sql("SELECT address, first_seen_block FROM wallets", conn)
        purchases = pd.read_sql(
            """
            SELECT p.coin, p.wallet, p.block, p.tx, c.chain
            FROM purchases p LEFT JOIN coins c ON c.address = p.coin
            """,
            conn,
        )
        verdicts = pd.read_sql(
            """
            SELECT v.coin, v.wallet, v.verdict, v.rule, v.reason, c.chain
            FROM coin_verdicts v LEFT JOIN coins c ON c.address = v.coin
            """,
            conn,
        )
        scores = pd.read_sql(
            """
            SELECT s.coin, s.wallet, s.winrate, s.net_pnl, s.trades, s.frequency, s.score, c.chain
            FROM coin_scores s LEFT JOIN coins c ON c.address = s.coin
            """,
            conn,
        )
    finally:
        conn.close()

    wallets["chain"] = (
        wallet_chain_map(purchases).reindex(wallets["address"]).fillna(UNKNOWN_CHAIN).to_numpy()
    )
    if "chain" not in coins.columns:
        coins["chain"] = None
    coins["chain"] = with_chain(coins)["chain"]
    return coins, wallets, with_chain(purchases), with_chain(verdicts), with_chain(scores)


def _fmt_scores(show: pd.DataFrame) -> pd.DataFrame:
    show = show.copy()
    for col, fmt in (("net_pnl", "{:+.2f}"), ("winrate", "{:.0%}"), ("frequency", "{:.6f}")):
        if col in show.columns:
            show[col] = show[col].map(lambda v, f=fmt: "" if pd.isna(v) else f.format(v))
    return show


def main() -> None:
    """Panel giriş noktası."""
    st.title("🏹 Avımsın — Smart Wallet Paneli")

    coins, wallets, purchases, verdicts, scores = load_data()
    if coins.empty:
        st.warning(
            "Veritabanı boş. Önce şunları çalıştırın:\n\n"
            "```bash\n"
            "uv run avimsin-scan <token> --from-block <blok>\n"
            "uv run avimsin-filter <token>\n"
            "uv run avimsin-score <token>\n"
            "```"
        )
        return

    options = chain_options(coins, wallets)
    picked = st.sidebar.radio("Zincir", ["Tümü", *options], horizontal=False)
    chain = None if picked == "Tümü" else picked
    coins, wallets, purchases, verdicts, scores = filter_by_chain(
        chain, coins, wallets, purchases, verdicts, scores
    )
    coin_pick = st.sidebar.selectbox("Coin", ["Tümü", *coin_options(coins)])

    if not has_coin_results(verdicts, scores):
        st.warning(LEGACY_RESULTS_NOTE)

    if picked != "Tümü":
        st.caption(f"Zincir filtresi: **{picked}** — {len(wallets)} cüzdan, {len(coins)} coin")
    if wallets.empty:
        st.info("Bu zincirde kayıtlı cüzdan yok.")
        return

    if coin_pick == "Tümü":
        st.subheader("Smart Wallet Sıralaması (tüm coinler)")
        view = all_coins_view(purchases, verdicts, scores)
        view["is_bot"] = view["is_bot"].map(lambda b: "🤖" if b else "")
        view.columns = ["Cüzdan", "En iyi skor", "En iyi coin", "ok_coins", "dump", "bot", "Bot?"]
        st.dataframe(view, width="stretch", hide_index=True)
        st.download_button(
            "Listeyi indir (CSV)", view.to_csv(index=False).encode(),
            file_name="smart_wallets.csv", mime="text/csv",
        )
    else:
        st.subheader(f"Coin: {coin_pick}")
        view = coin_view(coin_pick, purchases, verdicts, scores)
        view["verdict"] = view["verdict"].map(lambda v: VERDICT_LABEL.get(v if isinstance(v, str) else None, v))
        counts = view["verdict"].value_counts()
        for col, (label, count) in zip(st.columns(max(len(counts), 1)), counts.items()):
            col.metric(label, int(count))
        st.dataframe(_fmt_scores(view), width="stretch", hide_index=True)

    with st.expander("Coin'ler"):
        st.dataframe(coins, width="stretch", hide_index=True)


main()
