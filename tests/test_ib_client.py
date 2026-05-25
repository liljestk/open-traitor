from __future__ import annotations

import asyncio
import importlib
import sys
import threading
import types
from types import SimpleNamespace


def test_live_limit_order_runs_on_ib_worker_thread(monkeypatch):
    call_threads: dict[str, int | list[int]] = {
        "qualify": [],
        "place": [],
    }

    class Stock:
        def __init__(self, symbol: str, exchange: str, currency: str):
            self.symbol = symbol
            self.exchange = exchange
            self.currency = currency
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
            contract.conId = 123
            return [contract]

        def placeOrder(self, _contract, _order):
            call_threads["place"].append(threading.get_ident())
            return SimpleNamespace(order=SimpleNamespace(orderId=987))

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
    assert call_threads["connect"] != caller_thread
    assert set(call_threads["qualify"]) == {call_threads["connect"]}
    assert set(call_threads["place"]) == {call_threads["connect"]}
