"""avimsin-watch: smart wallet token girişlerini izler, Telegram'a bildirir.

Kullanım::

    uv run avimsin-watch [--once] [--db <yol>]
"""

from __future__ import annotations

import argparse
import sys

from .alerts.telegram import TelegramClient
from .alerts.watcher import WatchRpcError, watch_loop
from .chains import open_chain
from .config import settings
from .storage.db import connect as db_connect


def main() -> None:
    """CLI giriş noktası."""
    parser = argparse.ArgumentParser(
        description="Smart wallet token girişlerini izler; transferler doğrulanmış alım değildir."
    )
    parser.add_argument(
        "--once", action="store_true", help="Tek tur tara ve çık (daemon modu için sürekli)"
    )
    parser.add_argument(
        "--chain", default="solana", choices=["robinhood", "solana"],
        help="Zincir seçimi (varsayılan: solana)",
    )
    parser.add_argument("--db", default="data/avimsin.sqlite", help="SQLite dosya yolu")
    args = parser.parse_args()

    conn = db_connect(args.db)
    try:
        telegram = TelegramClient()
        with open_chain(args.chain) as client:
            if args.once:
                sent = watch_loop(
                    client, conn, client, once=True, telegram=telegram, chain=args.chain
                )
                print(f"{sent} bildirim gönderildi.")
            else:
                window = settings.watch_max_slots_per_poll
                window_note = f", pencere sınırı: {window} aralık" if window else ""
                print(
                    f"İzleme başladı ({args.chain}, yoklama: {settings.watch_poll_seconds}s{window_note})."
                    " Ctrl+C ile durdurun."
                )
                watch_loop(client, conn, client, telegram=telegram, chain=args.chain)
    except WatchRpcError as exc:
        print(f"HATA: {exc}; çıkış kodu 1.", file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        print("\nİzleme durduruldu.")
    finally:
        conn.close()
