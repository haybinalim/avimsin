"""avimsin-watch: smart wallet alım sinyallerini izler, Telegram'a bildirir.

Kullanım::

    uv run avimsin-watch [--once] [--db <yol>]
"""

from __future__ import annotations

import argparse

from .alerts.telegram import TelegramClient
from .alerts.watcher import watch_loop
from .chains.robinhood import connect
from .storage.db import connect as db_connect


def main() -> None:
    """CLI giriş noktası."""
    parser = argparse.ArgumentParser(
        description="Smart wallet alım sinyallerini izler; eşik aşımında Telegram'a bildirir."
    )
    parser.add_argument(
        "--once", action="store_true", help="Tek tur tara ve çık (daemon modu için sürekli)"
    )
    parser.add_argument("--db", default="data/avimsin.sqlite", help="SQLite dosya yolu")
    args = parser.parse_args()

    conn = db_connect(args.db)
    try:
        telegram = TelegramClient()
        with connect() as client:
            if args.once:
                sent = watch_loop(client, conn, client, once=True, telegram=telegram)
                print(f"{sent} bildirim gönderildi.")
            else:
                print(
                    f"İzleme başladı (eşik: eşik değeri .env'de, yoklama: {30}s aralıkla)."
                    " Ctrl+C ile durdurun."
                )
                watch_loop(client, conn, client, telegram=telegram)
    except KeyboardInterrupt:
        print("\nİzleme durduruldu.")
    finally:
        conn.close()
