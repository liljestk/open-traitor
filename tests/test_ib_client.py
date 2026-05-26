from __future__ import annotations

import asyncio
import importlib
import sys
import threading
import types
from types import SimpleNamespace


def test_live_limit_order_runs_on_ib_worker_thread(monkeypatch):
    call_threads: dict[str, int | list[int]] = {
        "account": [],
        "qualify": [],
        "place": [],
    }
    placed_contracts: list[object] = []

    class Stock:
        def __init__(
            self,
            symbol: str,
            exchange: str,
            currency: str,
            primaryExchange: str | None = None,
        ):
            self.symbol = symbol
            self.exchange = exchange
            self.currency = currency
            self.primaryExchange = primaryExchange
            self.conId = 0

    class LimitOrder:
        def __init__(self, action: str, totalQuantity: int, lmtPrice: float):
            self.action = action
            self.totalQuantity = totalQuantity
            self.lmtPrice = lmtPrice

    class MarketOrder:
        def __init__(self, action: str, totalQuantity: int):
            self.action = action
            self.totalQuantity = totalQuantity

    class FakeIB:
        def connect(self, *_args, **_kwargs):
            call_threads["connect"] = threading.get_ident()

        def qualifyContracts(self, contract):
            call_threads["qualify"].append(threading.get_ident())
            if (
                contract.symbol == "ENEL"
                and contract.currency == "EUR"
                and contract.primaryExchange == "BVME"
            ):
                contract.conId = 123
                return [contract]
            return []

        def placeOrder(self, contract, _order):
            call_threads["place"].append(threading.get_ident())
            placed_contracts.append(contract)
            return SimpleNamespace(
                order=SimpleNamespace(orderId=987),
                orderStatus=SimpleNamespace(status="Submitted", filled=0, avgFillPrice=0),
                log=[],
            )

        def accountValues(self, *_args):
            call_threads["account"].append(threading.get_ident())
            return [
                SimpleNamespace(
                    tag="NetLiquidationByCurrency",
                    currency="USD",
                    value="123.45",
                ),
                SimpleNamespace(
                    tag="NetLiquidationByCurrency",
                    currency="EUR",
                    value="123.45",
                ),
                SimpleNamespace(
                    tag="NetLiquidation",
                    currency="BASE",
                    value="123.45",
                )
            ]

        def sleep(self, _seconds):
            return None

    fake_ib_insync = types.ModuleType("ib_insync")
    fake_ib_insync.Stock = Stock
    fake_ib_insync.LimitOrder = LimitOrder
    fake_ib_insync.MarketOrder = MarketOrder
    monkeypatch.setitem(sys.modules, "ib_insync", fake_ib_insync)
    ib_mod = importlib.import_module("src.core.ib_client")
    monkeypatch.setattr(ib_mod, "_IB", FakeIB, raising=False)

    caller_thread = threading.get_ident()
    client = ib_mod.IBClient(
        paper_mode=False,
        ib_host="127.0.0.1",
        ib_port=4002,
        ib_client_id=991,
    )

    async def submit_limit_order():
        return client.place_limit_order("ENEL.MI-EUR", "BUY", 9.67, 1)

    result = asyncio.run(submit_limit_order())

    assert result["success"] is True
    assert result["order_id"] == "987"
    assert placed_contracts[0].symbol == "ENEL"
    assert placed_contracts[0].currency == "EUR"
    assert placed_contracts[0].primaryExchange == "BVME"
    assert call_threads["connect"] != caller_thread
    assert set(call_threads["qualify"]) == {call_threads["connect"]}
    assert set(call_threads["place"]) == {call_threads["connect"]}

    assert client.get_portfolio_value() == 123.45
    assert set(call_threads["account"]) == {call_threads["connect"]}


def test_live_limit_order_requires_broker_ack(monkeypatch):
    class Stock:
        def __init__(
            self,
            symbol: str,
            exchange: str,
            currency: str,
            primaryExchange: str | None = None,
        ):
            self.symbol = symbol
            self.exchange = exchange
            self.currency = currency
            self.primaryExchange = primaryExchange
            self.conId = 0

    class LimitOrder:
        def __init__(self, action: str, totalQuantity: int, lmtPrice: float):
            self.action = action
            self.totalQuantity = totalQuantity
            self.lmtPrice = lmtPrice

    class MarketOrder:
        def __init__(self, action: str, totalQuantity: int):
            self.action = action
            self.totalQuantity = totalQuantity

    class FakeIB:
        def connect(self, *_args, **_kwargs):
            return None

        def qualifyContracts(self, contract):
            contract.conId = 123
            return [contract]

        def placeOrder(self, _contract, _order):
            return SimpleNamespace(
                order=SimpleNamespace(orderId=988),
                orderStatus=SimpleNamespace(status="PendingSubmit", filled=0, avgFillPrice=0),
                log=[],
            )

    fake_ib_insync = types.ModuleType("ib_insync")
    fake_ib_insync.Stock = Stock
    fake_ib_insync.LimitOrder = LimitOrder
    fake_ib_insync.MarketOrder = MarketOrder
    monkeypatch.setitem(sys.modules, "ib_insync", fake_ib_insync)
    ib_mod = importlib.import_module("src.core.ib_client")
    monkeypatch.setattr(ib_mod, "_IB", FakeIB, raising=False)
    monkeypatch.setattr(ib_mod, "_IB_ORDER_ACK_TIMEOUT_SECONDS", 0.0)

    client = ib_mod.IBClient(
        paper_mode=False,
        ib_host="127.0.0.1",
        ib_port=4002,
        ib_client_id=992,
    )

    result = client.place_limit_order("ENEL.MI-EUR", "BUY", 9.67, 1)

    assert result["success"] is False
    assert result["status"] == "PENDINGSUBMIT"
    assert "not acknowledged" in result["error"]
