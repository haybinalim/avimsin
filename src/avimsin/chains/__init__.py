"""chains paketi — zincir istemcileri ve adaptörleri.

CLI'lar `--chain` bayrağıyla zincir seçer; open_chain() o zincirin
ChainClient sözleşmesini dolduran adaptörünü açar.
"""

from __future__ import annotations

CHAIN_CHOICES = ("robinhood", "solana")
# Kullanıcıya dönük CLI varsayılanı (kütüphane varsayılanı filters.chains.DEFAULT_CHAIN ayrıdır).
DEFAULT_CLI_CHAIN = "solana"


def open_chain(chain: str):
    """Seçilen zincirin ChainClient adaptörünü açar (bağlantıyı doğrular)."""
    if chain == "robinhood":
        from .robinhood import connect

        return connect()
    if chain == "solana":
        from .solana import connect

        return connect()
    raise ValueError(f"Bilinmeyen zincir: {chain!r} (bilinenler: {', '.join(CHAIN_CHOICES)})")


def attach_tx_cache(client, db_path: str) -> None:
    """Adaptör işlem önbelleği destekliyorsa (Solana) DB dosyasına bağlar; EVM'de no-op."""
    attach = getattr(client, "attach_tx_cache", None)
    if attach is not None:
        attach(db_path)


def report_unreadable(client) -> None:
    """Okunamayıp atlanan işlem varsa UYARI basar (stdout) ve ilk 5 imzayı stderr'e yazar."""
    skipped = list(getattr(client, "unreadable", None) or [])
    if not skipped:
        return
    import sys

    print(f"UYARI: {len(skipped)} işlem RPC'den okunamadı (atlandı)")
    print("atlanan imzalar: " + ", ".join(skipped[:5]) + (" ..." if len(skipped) > 5 else ""), file=sys.stderr)


def check_address_matches_chain(address: str, chain: str) -> None:
    """Adres biçimi seçilen zincirle uyuşmuyorsa RPC'den ÖNCE net hata verir.

    0x önekli adres EVM'dir (robinhood); aksi halde base58 (solana). Varsayılan
    zincir solana olduğundan --chain vermeden 0x adres sessizce Solana'da
    aranmasın diye bu koruma tüm CLI'larca (check_coin_chain üzerinden) kullanılır.
    """
    is_evm = address[:2].lower() == "0x"
    if chain == "solana" and is_evm:
        raise ValueError(f"{address} EVM adresi; --chain robinhood kullanın.")
    if chain == "robinhood" and not is_evm:
        raise ValueError(f"{address} EVM adresi değil (base58/Solana); --chain solana kullanın.")
