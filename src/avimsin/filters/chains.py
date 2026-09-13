"""Zincir zaman çizelgesi — filtre pencerelerinin süre karşılığı.

Tek kaynak: her zincirin blok/slot üretim aralığı (saniye). Kurallar süre
eşiğini bu aralığa bölerek aralık sayısına çevirir; zincir hızı değişirse
(kural eşikleri değil) burası güncellenir.

Kaynaklar:
- Solana: ~400 ms slot hedefi (SIMD-0175 sonrası fiilî ~400 ms; SIMD-0525 ile
  kademeli 200 ms hedefi — aralık 0.4 sn varsayımı korunur, hızlanırsa burası iner).
- Robinhood Chain: Arbitrum Orbit — ~100 ms yumuşak onay (soft confirmation).
  Filtre pencereleri için anlamlı ölçü sonlanmış bloktur; Orbit batch'leri
  L1'e dakikalar mertebesinde postalanır ama filtre "insan mı bot mu" sorusunu
  sorar: 10 blok = 1 sn altı insan-dışı hızdır. Varsayılan 0.25 sn, iki
  kaynağın arası, muhafazakâr tarafta tutulur.
"""

from __future__ import annotations

# Zincir adı → blok/slot aralığı (saniye). Adlar `chains.open_chain` ile aynı.
BLOCK_INTERVAL_SECONDS: dict[str, float] = {
    "robinhood": 0.25,
    "solana": 0.4,
}

DEFAULT_CHAIN = "robinhood"


def normalize_chain(chain: str) -> str:
    """Zincir adını doğrular, küçük harfe çevirir; bilinmeyende ValueError."""
    name = chain.lower()
    if name not in BLOCK_INTERVAL_SECONDS:
        known = ", ".join(sorted(BLOCK_INTERVAL_SECONDS))
        raise ValueError(f"bilinmeyen zincir: {chain!r} (bilinenler: {known})")
    return name
