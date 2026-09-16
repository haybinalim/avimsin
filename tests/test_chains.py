"""Adaptör sözleşmesi testleri — EVM ve Solana aynı ChainClient yüzeyini verir."""

from __future__ import annotations

import json
import time

import httpx
import pytest

from avimsin.chains import base as evm_base
from avimsin.chains.base import EvmAdapter, EvmClient
from avimsin.chains.solana import SYSTEM_PROGRAM, SolanaAdapter, SolanaClient
from avimsin.collectors.early_buyers import earliest_buyers
from avimsin.collectors.transfers import ZERO_ADDRESS
from tests.fake_evm import FakeEvmChain
from tests.fake_solana import FakeSolanaChain, make_balance_tx

TOKEN = "0x" + "aa" * 20
ALICE = "0x" + "11" * 20
BOB = "0x" + "22" * 20
CAROL = "0x" + "33" * 20
CONTRACT = "0x" + "44" * 20

MINT = "Mint111111111111111111111111111111111111111"
S_ALICE = "Alice1111111111111111111111111111111111111"
S_BOB = "Bob1111111111111111111111111111111111111111"
S_CAROL = "Carol11111111111111111111111111111111111111"


def evm_adapter(**kw) -> EvmAdapter:
    chain = FakeEvmChain(
        transfers=[
            (ZERO_ADDRESS, ALICE, 100, 1000),  # mint: erken alıcı değil
            (ALICE, BOB, 101, 400),
            (BOB, CAROL, 102, 100),
        ],
        token=TOKEN,
        latest_block=200,
        contracts={CONTRACT},
    )
    return EvmAdapter(EvmClient("http://sahte", transport=chain.transport()))




def test_evm_adapter_transfers_chronological_with_token_field() -> None:
    adapter = evm_adapter()
    events = adapter.token_transfers(TOKEN, 100, 200)
    # Adaptör ham olayları döndürür; mint eleme earliest_buyers'ın işidir.
    assert [(e.frm, e.to) for e in events] == [
        (ZERO_ADDRESS, ALICE),
        (ALICE, BOB),
        (BOB, CAROL),
    ]
    assert all(e.token == TOKEN for e in events)
    assert [e.block for e in events] == [100, 101, 102]



def test_evm_adapter_contract_and_chain_surface() -> None:
    adapter = evm_adapter()
    assert adapter.block_number() == 200
    assert adapter.decimals(TOKEN) == 18
    assert adapter.is_contract(CONTRACT) is True
    assert adapter.is_contract(BOB) is False


def test_earliest_buyers_works_through_protocol() -> None:
    adapter = evm_adapter()
    buyers = earliest_buyers(adapter, TOKEN, 100, 10)
    assert [b.wallet for b in buyers] == [BOB, CAROL]
    assert buyers[0].block == 101


@pytest.mark.parametrize("timeout_start", [1, 5])
def test_evm_timeout_splits_preserve_every_log_in_order(monkeypatch, timeout_start):
    monkeypatch.setattr(evm_base, "LOG_CHUNK", 4)
    monkeypatch.setattr(evm_base, "CHUNK_DELAY", 0)

    def get_logs(self, start, end, topics, address):
        if start >= timeout_start and start < end:
            raise RuntimeError("query timed out")
        return [{"block": block} for block in range(start, end + 1)]

    monkeypatch.setattr(EvmClient, "get_logs", get_logs)
    with EvmClient("http://unused") as client:
        assert list(client.iter_logs(1, 10, [])) == [
            {"block": block} for block in range(1, 11)
        ]


def test_evm_single_block_timeout_remains_visible(monkeypatch):
    def get_logs(self, start, end, topics, address):
        raise RuntimeError("query timed out")

    monkeypatch.setattr(EvmClient, "get_logs", get_logs)
    with EvmClient("http://unused") as client:
        with pytest.raises(RuntimeError, match="query timed out"):
            list(client.iter_logs(1, 1, []))


def sol_adapter(
    txs: dict[str, dict],
    sigs: list[dict],
    owners: dict | None = None,
    decimals: int = 9,
) -> SolanaAdapter:
    chain = FakeSolanaChain(
        signatures=sigs, transactions=txs, owners=owners, decimals=decimals
    )
    return SolanaAdapter(SolanaClient("http://sahte", transport=chain.transport(), min_interval=0.0))


def sig(name: str, slot: int, err: dict | None = None) -> dict:
    return {"signature": name, "slot": slot, "err": err}


def test_solana_single_transfer() -> None:
    txs = {
        "sig1": make_balance_tx(MINT, [(S_ALICE, 100)], [(S_BOB, 100)]),
    }
    adapter = sol_adapter(txs, [sig("sig1", 10)])
    events = adapter.token_transfers(MINT, 1, 100)
    assert len(events) == 1
    e = events[0]
    assert (e.frm, e.to, e.block, e.tx, e.value, e.token) == (
        S_ALICE, S_BOB, 10, "sig1", 100, MINT,
    )


def test_solana_mint_and_burn_marked_zero() -> None:
    txs = {
        "mint1": make_balance_tx(MINT, [], [(S_ALICE, 500)]),
        "burn1": make_balance_tx(MINT, [(S_ALICE, 200)], []),
    }
    adapter = sol_adapter(txs, [sig("burn1", 12), sig("mint1", 11)])
    events = adapter.token_transfers(MINT, 1, 100)
    by_tx = {e.tx: e for e in events}
    assert by_tx["mint1"].frm == ZERO_ADDRESS
    assert by_tx["mint1"].to == S_ALICE
    assert by_tx["burn1"].frm == S_ALICE
    assert by_tx["burn1"].to == ZERO_ADDRESS


def test_solana_failed_tx_skipped_and_slot_filtered() -> None:
    txs = {
        "bad": make_balance_tx(MINT, [(S_ALICE, 50)], [(S_BOB, 50)]),
        "good": make_balance_tx(MINT, [(S_ALICE, 70)], [(S_BOB, 70)]),
    }
    adapter = sol_adapter(
        txs, [sig("good", 20), sig("bad", 21, err={"InstructionError": [0, "Custom"]})]
    )
    events = adapter.token_transfers(MINT, 1, 100)
    assert [e.tx for e in events] == ["good"]
    # aralık dışı slot elenir
    assert adapter.token_transfers(MINT, 30, 100) == []


def test_solana_greedy_match_preserves_wallet_nets() -> None:
    # A:-100, B:-50, C:+150 — toplamlar korunmalı, karşıt kimliği tahminidir
    txs = {
        "multi": make_balance_tx(
            MINT, [(S_ALICE, 100), (S_BOB, 50)], [(S_CAROL, 150)]
        ),
    }
    adapter = sol_adapter(txs, [sig("multi", 5)])
    events = adapter.token_transfers(MINT, 1, 100)
    net: dict[str, int] = {}
    for e in events:
        if e.frm != ZERO_ADDRESS:
            net[e.frm] = net.get(e.frm, 0) - e.value
        if e.to != ZERO_ADDRESS:
            net[e.to] = net.get(e.to, 0) + e.value
    assert net == {S_ALICE: -100, S_BOB: -50, S_CAROL: 150}
    assert sum(e.value for e in events) == 150


def test_solana_owner_fallback_to_account_keys() -> None:
    txs = {
        "sig1": make_balance_tx(MINT, [(S_ALICE, 40)], [(S_BOB, 40)], with_owner=False),
    }
    adapter = sol_adapter(txs, [sig("sig1", 7)])
    events = adapter.token_transfers(MINT, 1, 100)
    assert [(e.frm, e.to) for e in events] == [(S_ALICE, S_BOB)]


def test_solana_is_contract_decimals_slot() -> None:
    adapter = sol_adapter(
        {}, [], owners={S_ALICE: SYSTEM_PROGRAM, S_BOB: "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"},
        decimals=6,
    )
    assert adapter.is_contract(S_ALICE) is False
    assert adapter.is_contract(S_BOB) is True
    assert adapter.is_contract("olmayan") is False
    assert adapter.decimals(MINT) == 6
    assert adapter.block_number() == 1_000


def _client_with_handler(handler) -> SolanaClient:
    return SolanaClient(
        "http://sahte",
        transport=httpx.MockTransport(handler),
        min_interval=0.0,
        rate_limit_retries=5,
        rate_limit_wait=0.01,
    )


def test_solana_client_429_kova_gecince_surdurur() -> None:
    """429 kovası soğuyunca aynı çağrı sonuçla döner (canlı mainnet gözlemi)."""

    hits = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.read())
        hits["n"] += 1
        if hits["n"] < 3:
            return httpx.Response(429, json={"jsonrpc": "2.0", "error": {"code": -32029}, "id": payload["id"]})
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": payload["id"], "result": 41})

    client = _client_with_handler(handler)
    assert client.call("getSlot", []) == 41
    assert hits["n"] == 3  # 1 asıl + 2 kova beklemesi


def test_solana_client_429_kaliciysa_runtime_error() -> None:
    """Kova hiç geçmezse RuntimeError yükselir — sayısız bekleme yok."""

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.read())
        return httpx.Response(429, json={"jsonrpc": "2.0", "error": {"code": -32029}, "id": payload["id"]})

    client = _client_with_handler(handler)
    client.rate_limit_retries = 1
    with pytest.raises(RuntimeError, match="429"):
        client.call("getSlot", [])

def test_solana_v1_tx_parses_like_v0() -> None:
    """v1 işlem v0 ile aynı delta'ları üretir (canlı mainnet; sürüm üst seviyededir)."""
    tx_v0 = make_balance_tx(MINT, [(S_ALICE, 100)], [(S_BOB, 100)])
    tx_v1 = {
        "slot": 12,
        "transaction": tx_v0["transaction"],
        "meta": tx_v0["meta"],
        "version": 1,  # gerçek RPC yanıtında sürüm sonuç kökündedir
    }
    adapter = sol_adapter({"v0sig": tx_v0, "v1sig": tx_v1}, [sig("v1sig", 12), sig("v0sig", 11)])
    assert [(e.frm, e.to, e.value) for e in adapter.token_transfers(MINT, 1, 100)] == [
        (S_ALICE, S_BOB, 100),
        (S_ALICE, S_BOB, 100),
    ]

def test_solana_client_paces_normal_calls() -> None:
    """Ardışık normal çağrılar min_interval kadar aralanır (ölçülü)."""

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.read())
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": payload["id"], "result": 7})

    client = SolanaClient(
        "http://sahte", transport=httpx.MockTransport(handler), min_interval=0.05
    )
    start = time.monotonic()
    assert client.call("getSlot", []) == 7
    assert client.call("getSlot", []) == 7
    assert time.monotonic() - start >= 0.04


def test_solana_client_paces_after_429_recovery() -> None:
    """429 iç-retry'si bekler; kova sonrası çağrı da aralanır (ölçülü)."""
    hits = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.read())
        hits["n"] += 1
        if hits["n"] == 1:
            return httpx.Response(429, json={"jsonrpc": "2.0", "error": {"code": -32029}, "id": payload["id"]})
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": payload["id"], "result": 9})

    client = SolanaClient(
        "http://sahte",
        transport=httpx.MockTransport(handler),
        min_interval=0.05,
        rate_limit_retries=3,
        rate_limit_wait=0.05,
    )
    start = time.monotonic()
    assert client.call("getSlot", []) == 9  # 1 asıl + 1 kova beklemesi
    assert hits["n"] == 2
    assert time.monotonic() - start >= 0.04
    # Kova çıkışındaki retry _last_call'u tazeledi; sonraki çağrı aralanır.
    # (Tazeleme yoksa bu çağrı beklemez, süre ~0 olurdu.)
    start = time.monotonic()
    assert client.call("getSlot", []) == 9
    assert time.monotonic() - start >= 0.04


def test_solana_get_transaction_sends_max_supported_1() -> None:
    """İstemci sürüm 1 ister; 0 isteyen reddedilir (gerçek RPC -32015 modeli)."""
    seen = {}
    tx_v1 = {
        "slot": 12,
        "transaction": make_balance_tx(MINT, [(S_ALICE, 100)], [(S_BOB, 100)])["transaction"],
        "meta": make_balance_tx(MINT, [(S_ALICE, 100)], [(S_BOB, 100)])["meta"],
        "version": 1,
    }

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.read())
        max_supported = payload["params"][1].get("maxSupportedTransactionVersion")
        seen["max_supported"] = max_supported
        if max_supported != 1:
            return httpx.Response(
                200,
                json={
                    "jsonrpc": "2.0",
                    "error": {"code": -32015, "message": "Transaction version (1) is not supported"},
                    "id": payload["id"],
                },
            )
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": payload["id"], "result": tx_v1})

    client = SolanaClient("http://sahte", transport=httpx.MockTransport(handler), min_interval=0.0)
    assert client.get_transaction("v1sig")["version"] == 1
    assert seen["max_supported"] == 1
    with pytest.raises(RuntimeError, match="-32015"):
        client.call("getTransaction", ["v1sig", {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0}])
