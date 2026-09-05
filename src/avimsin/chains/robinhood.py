"""Robinhood Chain adaptörü — Arbitrum Orbit L2, chain id 4663.

Kaynak: https://docs.robinhood.com/chain/connecting/
"""

from __future__ import annotations

from ..config import settings
from .base import EvmClient

CHAIN_ID = 4663

# Resmî public uç — rate limitli; üretim için Alchemy/QuickNode önerilir.
PUBLIC_RPC = "https://rpc.mainnet.chain.robinhood.com"


def resolve_rpc() -> str:
    """.env'deki RPC_ROBINHOOD_* uçlarından ilk doluyu seçer; yoksa public ucu döndürür."""
    for name, url in sorted(settings.rpc_endpoints.items()):
        if name.startswith("robinhood"):
            return url
    return PUBLIC_RPC


def connect() -> EvmClient:
    """Robinhood Chain istemcisi açar ve zincir kimliğini doğrular."""
    client = EvmClient(resolve_rpc())
    actual = client.chain_id()
    if actual != CHAIN_ID:
        client.close()
        raise RuntimeError(
            f"Zincir kimliği {CHAIN_ID} beklenirken {actual} bulundu ({resolve_rpc()})"
        )
    return client
