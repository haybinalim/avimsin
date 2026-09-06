"""Avımsın dashboard — smart wallet listesini tarayıcıda gösterir.

Çalıştırma::

    uv run streamlit run dashboard/app.py
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pandas as pd
import streamlit as st

DB_PATH = Path("data/avimsin.sqlite")

VERDICT_LABEL = {
    "ok": "✅ Temiz",
    "bot": "🤖 Bot",
    "dump": "📉 Kaçak satış",
    None: "⏳ Filtrelenmedi",
}

st.set_page_config(page_title="Avımsın", page_icon="🏹", layout="wide")


@st.cache_data(ttl=30)
def load_data() -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """coins, wallets (skorlu) ve purchases tablolarını okur."""
    if not DB_PATH.exists():
        return pd.DataFrame(), pd.DataFrame(), pd.DataFrame()
    conn = sqlite3.connect(DB_PATH)
    try:
        coins = pd.read_sql("SELECT * FROM coins", conn)
        wallets = pd.read_sql(
            """
            SELECT w.address, w.first_seen_block, w.verdict, w.verdict_rule,
                   w.verdict_reason, s.winrate, s.net_pnl, s.trades, s.frequency, s.score
            FROM wallets w
            LEFT JOIN scores s ON s.wallet = w.address
            ORDER BY s.score DESC NULLS LAST
            """,
            conn,
        )
        purchases = pd.read_sql("SELECT * FROM purchases", conn)
    finally:
        conn.close()
    return coins, wallets, purchases


def main() -> None:
    """Panel giriş noktası."""
    st.title("🏹 Avımsın — Smart Wallet Paneli")

    coins, wallets, purchases = load_data()
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

    st.subheader("Cüzdan Durumu")
    verdict_counts = wallets["verdict"].value_counts(dropna=False).rename(index={None: "filtrelenmedi"})
    cols = st.columns(len(verdict_counts))
    for col, (verdict, count) in zip(cols, verdict_counts.items()):
        col.metric(VERDICT_LABEL.get(verdict, verdict), int(count))

    st.subheader("Smart Wallet Sıralaması")
    scored = wallets[(wallets["verdict"] == "ok") & wallets["score"].notna()].copy()
    unscored = wallets[(wallets["verdict"] == "ok") & wallets["score"].isna()].copy()
    if scored.empty:
        st.info("Filtreden geçen (verdict=ok) cüzdan için skor verisi yok.")
    else:
        show = scored[["address", "score", "winrate", "net_pnl", "trades", "frequency"]].copy()
        show["net_pnl"] = show["net_pnl"].map("{:+.2f}".format)
        show["winrate"] = show["winrate"].map("{:.0%}".format)
        show["frequency"] = show["frequency"].map("{:.6f}".format)
        show.columns = ["Cüzdan", "Skor", "Winrate", "Net P&L", "Satış", "Frekans"]
        st.dataframe(show, use_container_width=True, hide_index=True)

        st.download_button(
            "Smart wallet listesini indir (CSV)",
            show.to_csv(index=False).encode(),
            file_name="smart_wallets.csv",
            mime="text/csv",
        )

    if not unscored.empty:
        st.caption(
            "Skorsuz temiz cüzdanlar (henüz satış yapmadı, skorlanamaz): "
            + ", ".join(f"`{a[:10]}…{a[-6:]}`" for a in unscored["address"])
        )

    with st.expander("Tüm cüzdanlar (filtre sonuçlarıyla)"):
        all_show = wallets.copy()
        all_show["verdict"] = all_show["verdict"].map(lambda v: VERDICT_LABEL.get(v, v))
        st.dataframe(all_show, use_container_width=True, hide_index=True)


main()
