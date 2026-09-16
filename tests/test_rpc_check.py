"""RPC protocol/network isolation and secret-safe health reporting."""

from __future__ import annotations

import json
from types import SimpleNamespace

import httpx
import pytest

from avimsin import rpc_check


GENESIS = "5eykt4UsFv8P8NJdTREpY1vzqKqZKvdpKuc147dw2N9d"
OTHER_GENESIS = "EtWTRABZaYq6iMfeYKouRu166VU2xqa1"
SECRET_URL = "https://user:password@rpc.test/private-key?api-key=secret-token"


def test_solana_and_legacy_evm_can_share_a_url_without_sharing_protocol():
    methods = []
    replies = {
        "getGenesisHash": GENESIS,
        "getSlot": 123456,
        "eth_chainId": "0x1237",
        "eth_blockNumber": "0x64",
    }

    def handle(request):
        method = json.loads(request.content)["method"]
        methods.append(method)
        return httpx.Response(200, json={"result": replies[method]})

    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        solana = rpc_check.check_endpoint(client, "solana_mainnet", SECRET_URL)
        evm = rpc_check.check_endpoint(client, "alchemy", SECRET_URL)

    assert solana["ok"] and evm["ok"]
    assert (solana["protocol"], solana["network_id"], solana["height"]) == (
        "solana", GENESIS, 123456
    )
    assert (evm["protocol"], evm["network_id"], evm["height"]) == ("evm", 4663, 100)
    assert methods == ["getGenesisHash", "getSlot", "eth_chainId", "eth_blockNumber"]


def invoke(monkeypatch, endpoints, handler, durations):
    # Complete two-call probes have deterministic timings, with no sleeps/network.
    clock = []
    for index, duration in enumerate(durations):
        clock.extend([index * 10.0, index * 10.0 + duration])
    ticks = iter(clock)
    monkeypatch.setattr(rpc_check, "time", SimpleNamespace(perf_counter=lambda: next(ticks)))
    monkeypatch.setattr(rpc_check, "settings", SimpleNamespace(rpc_endpoints=endpoints))
    client = httpx.Client(transport=httpx.MockTransport(handler))
    monkeypatch.setattr(rpc_check.httpx, "Client", lambda: client)
    rpc_check.main()


@pytest.mark.parametrize(
    ("names", "networks", "methods"),
    [
        (("solana_first", "solana_second"), (GENESIS, OTHER_GENESIS),
         ("getGenesisHash", "getSlot")),
        (("alchemy", "quicknode"), ("0x1", "0x1237"),
         ("eth_chainId", "eth_blockNumber")),
    ],
)
def test_cli_distinct_networks_have_independent_fastest_and_no_lag(
    monkeypatch, capsys, names, networks, methods
):
    calls = []
    endpoints = {names[0]: "https://first.test", names[1]: "https://second.test"}

    def handle(request):
        method = json.loads(request.content)["method"]
        index = 0 if request.url.host == "first.test" else 1
        calls.append((index, method))
        height = 100 if index == 0 else 900000
        value = networks[index] if method == methods[0] else (
            height if method == "getSlot" else hex(height)
        )
        return httpx.Response(200, json={"result": value})

    invoke(monkeypatch, endpoints, handle, [0.1, 0.01])
    output = capsys.readouterr().out
    # Each endpoint has a table row and its own fastest summary; neither has a lag warning.
    assert output.count(names[0]) == 2
    assert output.count(names[1]) == 2
    assert calls == [(0, methods[0]), (0, methods[1]), (1, methods[0]), (1, methods[1])]
    for network in networks:
        assert str(int(network, 16) if network.startswith("0x") else network) in output


@pytest.mark.parametrize("protocol", ["evm", "solana"])
def test_cli_lag_threshold_and_fastest_are_local_to_network(monkeypatch, capsys, protocol):
    prefix = "solana_" if protocol == "solana" else "evm_"
    names = [prefix + suffix for suffix in ("lagging", "near", "tip", "other")]
    endpoints = {name: f"https://node{index}.test" for index, name in enumerate(names)}
    heights = [97, 98, 100, 999999]

    def handle(request):
        method = json.loads(request.content)["method"]
        index = int(request.url.host.removeprefix("node").removesuffix(".test"))
        if method == "getGenesisHash":
            value = GENESIS if index < 3 else OTHER_GENESIS
        elif method == "eth_chainId":
            value = "0x1" if index < 3 else "0x2"
        else:
            value = heights[index] if protocol == "solana" else hex(heights[index])
        return httpx.Response(200, json={"result": value})

    invoke(monkeypatch, endpoints, handle, [0.03, 0.04, 0.02, 0.01])
    output = capsys.readouterr().out
    assert output.count(names[0]) == 2  # row + lag warning (>2 behind local tip)
    assert output.count(names[1]) == 1  # two behind is not a warning
    assert output.count(names[2]) == 2  # row + local fastest, despite faster other network
    assert output.count(names[3]) == 2  # row + other network's fastest
    assert output.index(names[2]) < output.index(names[3])  # separate group, not global sort


def test_cli_same_url_keeps_protocol_groups_separate(monkeypatch, capsys):
    replies = {"getGenesisHash": GENESIS, "getSlot": 999999,
               "eth_chainId": "0x1", "eth_blockNumber": "0x64"}

    def handle(request):
        return httpx.Response(200, json={"result": replies[json.loads(request.content)["method"]]})

    invoke(monkeypatch, {"solana_shared": SECRET_URL, "alchemy_shared": SECRET_URL},
           handle, [0.1, 0.01])
    output = capsys.readouterr().out
    assert output.count("solana_shared") == 2
    assert output.count("alchemy_shared") == 2
    assert GENESIS in output
    assert SECRET_URL not in output


@pytest.mark.parametrize("failure", ["http", "transport", "rpc", "malformed", "bad_code"])
def test_cli_failures_hide_secrets_and_continue(monkeypatch, capsys, failure):
    reached = []

    def handle(request):
        method = json.loads(request.content)["method"]
        reached.append((request.url.host, method))
        if request.url.host == "rpc.test":
            if failure == "http":
                return httpx.Response(403, text=SECRET_URL)
            if failure == "transport":
                raise httpx.ConnectError(SECRET_URL, request=request)
            if failure == "rpc":
                return httpx.Response(200, json={"error": {"code": -32005, "message": SECRET_URL}})
            if failure == "bad_code":
                return httpx.Response(200, json={"error": {"code": SECRET_URL, "message": SECRET_URL}})
            return httpx.Response(200, text=SECRET_URL)
        return httpx.Response(200, json={"result": GENESIS if method == "getGenesisHash" else 100})

    monkeypatch.setattr(rpc_check, "settings", SimpleNamespace(rpc_endpoints={
        "broken": SECRET_URL, "solana_healthy": "https://healthy.test"
    }))
    client = httpx.Client(transport=httpx.MockTransport(handle))
    monkeypatch.setattr(rpc_check.httpx, "Client", lambda: client)
    rpc_check.main()
    output = capsys.readouterr().out
    assert "broken" in output
    assert output.count("solana_healthy") == 2
    assert reached[-2:] == [("healthy.test", "getGenesisHash"), ("healthy.test", "getSlot")]
    assert "eth_chainId" in output
    for secret in (SECRET_URL, "password", "private-key", "secret-token", "rpc.test"):
        assert secret not in output
    if failure == "http":
        assert "403" in output
    if failure == "rpc":
        assert "-32005" in output


@pytest.mark.parametrize(
    ("name", "replies"),
    [
        ("solana_bad", [{"result": None}]),
        ("solana_bad", [{"result": GENESIS}, {"result": True}]),
        ("solana_bad", [{"result": GENESIS}, {"result": -1}]),
        ("evm_bad", [{"result": SECRET_URL}]),
        ("evm_bad", [{"result": "0x1"}, {"result": None}]),
        ("evm_bad", [[]]),
        ("evm_bad", [{}]),
    ],
)
def test_invalid_rpc_results_are_not_healthy(name, replies):
    responses = iter(replies)
    with httpx.Client(transport=httpx.MockTransport(
        lambda request: httpx.Response(200, json=next(responses))
    )) as client:
        result = rpc_check.check_endpoint(client, name, SECRET_URL)
    assert not result["ok"]
    assert "height" not in result
    assert SECRET_URL not in result["error"]
