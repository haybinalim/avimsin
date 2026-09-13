"""chains paketi — zincir istemcileri ve adaptörleri.

CLI'lar `--chain` bayrağıyla zincir seçer; open_chain() o zincirin
ChainClient sözleşmesini dolduran adaptörünü açar.
"""

from __future__ import annotations

CHAIN_CHOICES = ("robinhood", "solana")


def open_chain(chain: str):
    """Seçilen zincirin ChainClient adaptörünü açar (bağlantıyı doğrular)."""
    if chain == "robinhood":
        from .robinhood import connect

        return connect()
    if chain == "solana":
        from .solana import connect

        return connect()
    raise ValueError(f"Bilinmeyen zincir: {chain!r} (bilinenler: {', '.join(CHAIN_CHOICES)})")
