#!/usr/bin/env python3
"""
IBKR API Connectivity Test Script.

Tests connection to IB Gateway/TWS running locally, validates:
  1. TCP connectivity to the gateway port
  2. ib_insync connection + managed accounts
  3. Account balances & positions
  4. Market data (price quotes)
  5. News provider discovery + historical news
  6. Scanner discovery

Usage:
    python scripts/test_ibkr.py
    python scripts/test_ibkr.py --host 127.0.0.1 --port 4002
"""

from __future__ import annotations

import argparse
import os
import socket
import sys
import time

# Add project root
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
load_dotenv("config/.env")


def test_tcp_connectivity(host: str, port: int) -> bool:
    """Basic TCP socket test."""
    print(f"\n{'='*60}")
    print(f"  1. TCP Connectivity Test → {host}:{port}")
    print(f"{'='*60}")
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(5)
        result = sock.connect_ex((host, port))
        sock.close()
        if result == 0:
            print(f"  ✅ Port {port} is OPEN on {host}")
            return True
        else:
            print(f"  ❌ Port {port} is CLOSED on {host} (error code: {result})")
            return False
    except Exception as e:
        print(f"  ❌ TCP test failed: {e}")
        return False


def test_ib_connection(host: str, port: int, client_id: int) -> bool:
    """Test ib_insync connection."""
    print(f"\n{'='*60}")
    print(f"  2. IB Gateway Connection Test")
    print(f"{'='*60}")
    try:
        from ib_insync import IB
        ib = IB()
        ib.connect(host, port, clientId=client_id, timeout=10)
        print(f"  ✅ Connected to IB Gateway at {host}:{port}")
        print(f"     Client ID: {client_id}")
        print(f"     Server version: {ib.client.serverVersion()}")

        # Managed accounts
        accounts = ib.managedAccounts()
        print(f"  ✅ Managed accounts: {accounts}")

        return True, ib
    except ImportError:
        print("  ❌ ib_insync not installed. Run: pip install ib_insync")
        return False, None
    except Exception as e:
        print(f"  ❌ IB connection failed: {e}")
        return False, None


def test_account_data(ib) -> bool:
    """Test account balance and position data."""
    print(f"\n{'='*60}")
    print(f"  3. Account Data Test")
    print(f"{'='*60}")
    try:
        # Account values
        vals = ib.accountValues()
        print(f"  📊 Total account values: {len(vals)}")
        
        # Find key values
        for v in vals:
            if v.tag in ("NetLiquidation", "CashBalance", "TotalCashValue", "NetLiquidationByCurrency"):
                print(f"     {v.tag}: {v.value} {v.currency}")

        # Positions
        positions = ib.positions()
        if positions:
            print(f"  📊 Open positions: {len(positions)}")
            for pos in positions:
                print(f"     {pos.contract.symbol}: {pos.position} shares @ avg {pos.avgCost:.2f}")
        else:
            print(f"  📊 No open positions (account may be cash-only)")

        return True
    except Exception as e:
        print(f"  ❌ Account data test failed: {e}")
        return False


def test_market_data(ib) -> bool:
    """Test fetching market data for a few common stocks."""
    print(f"\n{'='*60}")
    print(f"  4. Market Data Test")
    print(f"{'='*60}")
    try:
        from ib_insync import Stock

        test_pairs = ["AAPL", "MSFT", "NVDA"]
        for symbol in test_pairs:
            contract = Stock(symbol, 'SMART', 'EUR')
            try:
                ib.qualifyContracts(contract)
                tickers = ib.reqTickers(contract)
                if tickers:
                    t = tickers[0]
                    price = t.last if t.last == t.last and t.last > 0 else t.close
                    print(f"  📈 {symbol}-EUR: last={t.last}, close={t.close}, bid={t.bid}, ask={t.ask}")
                else:
                    # Try USD
                    contract_usd = Stock(symbol, 'SMART', 'USD')
                    ib.qualifyContracts(contract_usd)
                    tickers_usd = ib.reqTickers(contract_usd)
                    if tickers_usd:
                        t = tickers_usd[0]
                        print(f"  📈 {symbol}-USD: last={t.last}, close={t.close}, bid={t.bid}, ask={t.ask}")
                    else:
                        print(f"  ⚠️ {symbol}: No ticker data")
            except Exception as e:
                print(f"  ⚠️ {symbol}: {e}")

        return True
    except Exception as e:
        print(f"  ❌ Market data test failed: {e}")
        return False


def test_news(ib) -> bool:
    """Test news provider discovery and news fetching."""
    print(f"\n{'='*60}")
    print(f"  5. News Test")
    print(f"{'='*60}")
    try:
        # Discover providers
        providers = ib.reqNewsProviders()
        if providers:
            print(f"  📰 Available news providers: {len(providers)}")
            for p in providers:
                print(f"     {p.code}: {p.name}")
        else:
            print(f"  ⚠️ No news providers available (may need data subscription)")
            return True  # Not a failure — just no subscription

        # Try fetching news for AAPL
        from ib_insync import Stock
        contract = Stock("AAPL", "SMART", "USD")
        ib.qualifyContracts(contract)
        
        provider_codes = '+'.join(p.code for p in providers)
        news = ib.reqHistoricalNews(
            conId=contract.conId,
            providerCodes=provider_codes,
            startDateTime='',
            endDateTime='',
            totalResults=5
        )
        
        if news:
            print(f"  📰 News for AAPL: {len(news)} articles")
            for n in news[:3]:
                print(f"     [{n.providerCode}] {n.time}: {n.headline[:80]}...")
        else:
            print(f"  ℹ️ No news articles returned (may need market data subscription)")

        return True
    except Exception as e:
        print(f"  ❌ News test failed: {e}")
        return False


def test_scanner(ib) -> bool:
    """Test scanner (pair discovery)."""
    print(f"\n{'='*60}")
    print(f"  6. Scanner Test (Pair Discovery)")
    print(f"{'='*60}")
    try:
        from ib_insync import ScannerSubscription
        
        sub = ScannerSubscription(
            instrument='STK',
            locationCode='STK.US.MAJOR',
            scanCode='TOP_PERC_GAIN'
        )
        scan_data = ib.reqScannerData(sub)
        
        if scan_data:
            print(f"  🔍 Scanner found {len(scan_data)} stocks")
            for item in scan_data[:5]:
                sym = item.contractDetails.contract.symbol
                print(f"     {sym}")
        else:
            print(f"  ⚠️ Scanner returned no results")
        
        return True
    except Exception as e:
        print(f"  ❌ Scanner test failed: {e}")
        return False


def test_ibclient_wrapper(host: str, port: int, client_id: int) -> bool:
    """Test our IBClient wrapper."""
    print(f"\n{'='*60}")
    print(f"  7. IBClient Wrapper Test")
    print(f"{'='*60}")
    try:
        from src.core.ib_client import IBClient
        
        client = IBClient(
            paper_mode=False,
            ib_host=host,
            ib_port=port,
            ib_client_id=client_id + 5,
        )
        
        # check_connection
        conn = client.check_connection()
        print(f"  ✅ check_connection: {conn}")
        
        # balance
        bal = client.balance
        print(f"  💰 Balance: {bal}")
        
        # portfolio value
        pv = client.get_portfolio_value()
        print(f"  💰 Portfolio value: {pv}")
        
        # get_accounts
        accounts = client.get_accounts()
        print(f"  📊 Accounts: {len(accounts)}")
        for acc in accounts:
            print(f"     {acc}")
        
        # get_news
        news = client.get_news("AAPL-EUR", limit=3)
        print(f"  📰 News for AAPL: {len(news)} articles")

        # get_news_providers
        providers = client.get_news_providers()
        print(f"  📰 News providers: {providers}")
        
        # Disconnect
        if hasattr(client, 'ib') and client.ib.isConnected():
            client.ib.disconnect()
        
        return True
    except Exception as e:
        print(f"  ❌ IBClient test failed: {e}")
        return False


def main():
    parser = argparse.ArgumentParser(description="Test IBKR API Connectivity")
    parser.add_argument("--host", default=os.environ.get("IBKR_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("IBKR_PORT", "4002")))
    parser.add_argument("--client-id", type=int, default=int(os.environ.get("IBKR_CLIENT_ID", "1")))
    args = parser.parse_args()

    print(f"\n{'#'*60}")
    print(f"  IBKR API Connectivity Test Suite")
    print(f"  Host: {args.host}:{args.port}  Client ID: {args.client_id}")
    print(f"{'#'*60}")

    results = {}

    # Test 1: TCP
    results["tcp"] = test_tcp_connectivity(args.host, args.port)
    if not results["tcp"]:
        print("\n❌ TCP connectivity failed. Is IB Gateway running?")
        print("   Check: IB Gateway → Configure → API → Settings")
        print("   - Enable ActiveX and Socket Clients ✓")
        print("   - Socket port: 4002 (paper) or 4001 (live)")
        print("   - Trusted IPs: 127.0.0.1 (add Docker subnet if needed)")
        sys.exit(1)

    # Test 2: IB Connection
    success, ib = test_ib_connection(args.host, args.port, args.client_id)
    results["connection"] = success
    if not success or ib is None:
        print("\n❌ IB connection failed. Check API settings in IB Gateway.")
        sys.exit(1)

    try:
        # Test 3: Account data
        results["account"] = test_account_data(ib)

        # Test 4: Market data
        results["market_data"] = test_market_data(ib)

        # Test 5: News
        results["news"] = test_news(ib)

        # Test 6: Scanner
        results["scanner"] = test_scanner(ib)
    finally:
        ib.disconnect()

    # Test 7: IBClient wrapper (separate connection)
    results["ibclient"] = test_ibclient_wrapper(args.host, args.port, args.client_id)

    # Summary
    print(f"\n{'='*60}")
    print(f"  TEST RESULTS SUMMARY")
    print(f"{'='*60}")
    for test_name, passed in results.items():
        status = "✅ PASS" if passed else "❌ FAIL"
        print(f"  {status} — {test_name}")
    
    all_passed = all(results.values())
    print(f"\n  {'✅ ALL TESTS PASSED' if all_passed else '⚠️ SOME TESTS FAILED'}")
    print()
    
    sys.exit(0 if all_passed else 1)


if __name__ == "__main__":
    main()
