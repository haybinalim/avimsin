"""Request pacing and bounded retry transitions, without wall-clock sleeps."""
import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from avimsin.chains import solana


@pytest.fixture
def clock(monkeypatch):
    now = [0.0]
    def sleep(seconds):
        now[0] += seconds
    monkeypatch.setattr(solana, "time", SimpleNamespace(monotonic=lambda: now[0], sleep=sleep))
    return now


def test_all_attempts_and_next_call_are_paced(clock):
    responses = iter([429, 429, 200, 200])
    starts = []
    def handle(request):
        starts.append(clock[0])
        return httpx.Response(next(responses), json={"result": 7})
    with solana.SolanaClient("http://rpc", transport=httpx.MockTransport(handle),
                             min_interval=1, rate_limit_wait=0.1) as client:
        assert client.get_slot() == 7
        assert client.get_slot() == 7
    assert starts == [0, 1, 2, 3]


@pytest.mark.parametrize("middle", [503, "transport"])
def test_rate_limit_transition_recovers(clock, middle):
    responses = iter([429, middle, 200])
    def handle(request):
        status = next(responses)
        if status == "transport":
            raise httpx.ReadTimeout("temporary")
        return httpx.Response(status, json={"result": 19})
    with solana.SolanaClient("http://rpc", transport=httpx.MockTransport(handle),
                             min_interval=0, rate_limit_wait=0) as client:
        assert client.get_slot() == 19


@pytest.mark.parametrize("failure", [429, 503, "transport"])
def test_persistent_failures_are_bounded(clock, failure):
    calls = []
    def handle(request):
        calls.append(request)
        if failure == "transport":
            raise httpx.ConnectError("temporary")
        return httpx.Response(failure, json={"result": None})
    with solana.SolanaClient("http://rpc", transport=httpx.MockTransport(handle),
                             min_interval=0, rate_limit_wait=0, rate_limit_retries=2) as client:
        with pytest.raises(RuntimeError):
            client.get_slot()
    assert len(calls) == 3


def test_mixed_failures_do_not_reset_budgets(clock):
    responses = iter([429, 503, 429, 503, 429])
    calls = []
    def handle(request):
        calls.append(request)
        return httpx.Response(next(responses), json={"result": None})
    with solana.SolanaClient("http://rpc", transport=httpx.MockTransport(handle),
                             min_interval=0, rate_limit_wait=0, rate_limit_retries=2) as client:
        with pytest.raises(RuntimeError, match="429"):
            client.get_slot()
    assert len(calls) == 5


def test_version_one_contract_decodes_token_movement():
    evidence = json.loads((Path(__file__).parent / "fixtures/solana_v1.json").read_text())
    tx, mint = evidence["transaction"], evidence["mint"]
    slot, signature = tx["slot"], evidence["signature"]
    def handle(request):
        payload = json.loads(request.content)
        if payload["method"] == "getSignaturesForAddress":
            return httpx.Response(200, json={"result": [] if "before" in payload["params"][1]
                                           else [{"slot": slot, "signature": signature, "err": None}]})
        if payload["params"][1].get("maxSupportedTransactionVersion", -1) < 1:
            return httpx.Response(200, json={"error": {"code": -32015}})
        return httpx.Response(200, json={"result": tx})
    with solana.SolanaClient("http://rpc", transport=httpx.MockTransport(handle), min_interval=0) as client:
        with pytest.raises(RuntimeError, match="-32015"):
            client.call("getTransaction", [signature, {"maxSupportedTransactionVersion": 0}])
        events = solana.SolanaAdapter(client).token_transfers(mint, slot, slot)
    assert [(e.frm, e.to, e.value) for e in events] == [
        ("FB5jAs5Q7XFLhoM6EePn1f8UqqvExnCiirdtEXpL7g9N",
         "DHhdyEj6QNESqgQqD9yMUoyCSHA2JPUz6rFjdnkd5t16", 435981729625)
    ]
