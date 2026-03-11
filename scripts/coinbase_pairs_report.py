#!/usr/bin/env python3
"""
Fetch all trading pairs from Coinbase List Products endpoint and generate
an interactive HTML report showing available pairs, grouped by quote currency.
"""

from __future__ import annotations

import html
import os
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

# Allow imports from project root
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv
from coinbase.rest import RESTClient

# Load env from config/.env (same as main.py)
_project_root = Path(__file__).resolve().parent.parent
load_dotenv(_project_root / "config" / ".env")


def fetch_all_products() -> list[dict]:
    """Fetch all products from Coinbase Advanced Trade API."""
    api_key = os.environ.get("COINBASE_API_KEY")
    api_secret = os.environ.get("COINBASE_API_SECRET")
    if api_key and api_secret:
        client = RESTClient(api_key=api_key, api_secret=api_secret)
    else:
        client = RESTClient()
    resp = client.get_products()
    data = resp.to_dict() if hasattr(resp, "to_dict") else dict(resp)
    return data.get("products", [])


def build_html(products: list[dict]) -> str:
    """Build an interactive HTML report from the product list."""

    # Separate online/tradeable from disabled
    online = []
    disabled = []
    for p in products:
        is_online = (
            not p.get("trading_disabled", True)
            and not p.get("is_disabled", False)
            and str(p.get("status", "")).lower() == "online"
        )
        if is_online:
            online.append(p)
        else:
            disabled.append(p)

    # Group online pairs by quote currency
    by_quote: dict[str, list[dict]] = defaultdict(list)
    for p in online:
        quote = p.get("quote_currency_id", "???")
        by_quote[quote].append(p)

    # Sort quotes: fiat first (USD, EUR, GBP...), then stablecoins, then crypto
    FIAT = {"USD", "EUR", "GBP", "CHF", "CAD", "AUD", "JPY", "SGD", "BRL", "MXN"}
    STABLE = {"USDC", "USDT", "PYUSD", "FDUSD", "DAI", "USDS", "EURC"}

    def quote_sort_key(q: str) -> tuple:
        if q in FIAT:
            return (0, q)
        if q in STABLE:
            return (1, q)
        return (2, q)

    sorted_quotes = sorted(by_quote.keys(), key=quote_sort_key)

    # Build base-to-quotes mapping for the matrix
    base_quotes: dict[str, set[str]] = defaultdict(set)
    for p in online:
        base_quotes[p.get("base_currency_id", "???")].add(
            p.get("quote_currency_id", "???")
        )

    # Count all unique quote currencies
    all_quotes = sorted({q for qs in base_quotes.values() for q in qs}, key=quote_sort_key)

    # Stats
    total_products = len(products)
    total_online = len(online)
    total_disabled = len(disabled)
    unique_bases = len({p.get("base_currency_id") for p in online})
    unique_quotes = len({p.get("quote_currency_id") for p in online})
    crypto_quotes = {q for q in all_quotes if q not in FIAT and q not in STABLE}

    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    # --- Build HTML sections ---

    # Quote currency tabs content
    tab_buttons = []
    tab_contents = []

    for idx, quote in enumerate(sorted_quotes):
        pairs = sorted(by_quote[quote], key=lambda p: p.get("base_currency_id", ""))
        active = "active" if idx == 0 else ""
        cat = "fiat" if quote in FIAT else ("stable" if quote in STABLE else "crypto")
        tab_buttons.append(
            f'<button class="tab-btn {active}" data-tab="tab-{quote}" data-cat="{cat}">'
            f'{html.escape(quote)} <span class="badge">{len(pairs)}</span></button>'
        )

        rows = []
        for p in pairs:
            pid = html.escape(p.get("product_id", ""))
            base = html.escape(p.get("base_currency_id", ""))
            price = p.get("price", "")
            try:
                price_f = f"{float(price):,.8g}"
            except (ValueError, TypeError):
                price_f = str(price)
            vol = p.get("volume_24h", "")
            try:
                vol_f = f"{float(vol):,.2f}"
            except (ValueError, TypeError):
                vol_f = str(vol)
            pct = p.get("price_percentage_change_24h", "")
            try:
                pct_f = float(pct)
                pct_class = "positive" if pct_f >= 0 else "negative"
                pct_str = f"{pct_f:+.2f}%"
            except (ValueError, TypeError):
                pct_class = ""
                pct_str = str(pct)

            rows.append(
                f"<tr>"
                f"<td class='pair-id'>{pid}</td>"
                f"<td>{base}</td>"
                f"<td class='num'>{price_f}</td>"
                f"<td class='num'>{vol_f}</td>"
                f"<td class='num {pct_class}'>{pct_str}</td>"
                f"</tr>"
            )

        display = "block" if idx == 0 else "none"
        tab_contents.append(
            f'<div class="tab-content" id="tab-{quote}" style="display:{display}">'
            f'<table class="pair-table">'
            f"<thead><tr>"
            f"<th>Pair</th><th>Base</th><th>Price ({html.escape(quote)})</th>"
            f"<th>Volume 24h</th><th>Change 24h</th>"
            f"</tr></thead><tbody>"
            + "\n".join(rows)
            + "</tbody></table></div>"
        )

    # Pair availability matrix (top 50 bases by number of quote options)
    top_bases = sorted(base_quotes.keys(), key=lambda b: -len(base_quotes[b]))[:60]
    matrix_rows = []
    for base in top_bases:
        cells = f"<td class='base-label'>{html.escape(base)}</td>"
        for q in all_quotes:
            if q in base_quotes[base]:
                cells += "<td class='has-pair' title='{}-{}'>&#10003;</td>".format(
                    html.escape(base), html.escape(q)
                )
            else:
                cells += "<td class='no-pair'></td>"
        matrix_rows.append(f"<tr>{cells}</tr>")

    matrix_header = "<th>Base \\ Quote</th>" + "".join(
        f"<th class='rotate'><div>{html.escape(q)}</div></th>" for q in all_quotes
    )

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Coinbase Available Trading Pairs</title>
<style>
  :root {{
    --bg: #0d1117; --surface: #161b22; --border: #30363d;
    --text: #e6edf3; --text-dim: #8b949e; --accent: #58a6ff;
    --green: #3fb950; --red: #f85149; --yellow: #d29922;
    --fiat-bg: #1a3a2a; --stable-bg: #1a2a3a; --crypto-bg: #2a1a3a;
  }}
  * {{ margin: 0; padding: 0; box-sizing: border-box; }}
  body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
         background: var(--bg); color: var(--text); padding: 24px; }}
  h1 {{ font-size: 1.6rem; margin-bottom: 4px; }}
  .subtitle {{ color: var(--text-dim); margin-bottom: 20px; font-size: 0.9rem; }}
  .stats {{ display: flex; gap: 16px; flex-wrap: wrap; margin-bottom: 24px; }}
  .stat-card {{ background: var(--surface); border: 1px solid var(--border);
               border-radius: 8px; padding: 14px 20px; min-width: 150px; }}
  .stat-card .label {{ color: var(--text-dim); font-size: 0.8rem; text-transform: uppercase; }}
  .stat-card .value {{ font-size: 1.5rem; font-weight: 600; margin-top: 4px; }}
  .section {{ margin-bottom: 32px; }}
  .section h2 {{ font-size: 1.2rem; margin-bottom: 12px; padding-bottom: 8px;
                 border-bottom: 1px solid var(--border); }}
  .tab-bar {{ display: flex; flex-wrap: wrap; gap: 6px; margin-bottom: 16px; }}
  .tab-btn {{ background: var(--surface); color: var(--text-dim); border: 1px solid var(--border);
             border-radius: 6px; padding: 6px 14px; cursor: pointer; font-size: 0.85rem;
             transition: all 0.15s; }}
  .tab-btn:hover {{ color: var(--text); border-color: var(--accent); }}
  .tab-btn.active {{ background: var(--accent); color: #fff; border-color: var(--accent); }}
  .tab-btn[data-cat="fiat"] {{ border-left: 3px solid var(--green); }}
  .tab-btn[data-cat="stable"] {{ border-left: 3px solid var(--accent); }}
  .tab-btn[data-cat="crypto"] {{ border-left: 3px solid var(--yellow); }}
  .badge {{ background: rgba(255,255,255,0.15); border-radius: 10px; padding: 1px 7px;
           font-size: 0.75rem; margin-left: 4px; }}
  .filter-bar {{ margin-bottom: 12px; }}
  .filter-bar input {{ background: var(--surface); border: 1px solid var(--border);
                       border-radius: 6px; padding: 8px 14px; color: var(--text);
                       font-size: 0.9rem; width: 300px; }}
  .pair-table {{ width: 100%; border-collapse: collapse; font-size: 0.85rem; }}
  .pair-table th {{ text-align: left; padding: 8px 12px; background: var(--surface);
                   color: var(--text-dim); border-bottom: 2px solid var(--border);
                   position: sticky; top: 0; }}
  .pair-table td {{ padding: 6px 12px; border-bottom: 1px solid var(--border); }}
  .pair-table tr:hover {{ background: rgba(88,166,255,0.06); }}
  .pair-id {{ font-weight: 600; color: var(--accent); }}
  .num {{ text-align: right; font-variant-numeric: tabular-nums; }}
  .positive {{ color: var(--green); }}
  .negative {{ color: var(--red); }}
  /* Matrix */
  .matrix-wrap {{ overflow-x: auto; max-height: 70vh; }}
  .matrix {{ border-collapse: collapse; font-size: 0.75rem; }}
  .matrix th, .matrix td {{ padding: 3px 6px; border: 1px solid var(--border); text-align: center; }}
  .matrix th {{ background: var(--surface); position: sticky; top: 0; z-index: 2; }}
  .matrix th.rotate {{ white-space: nowrap; }}
  .matrix th.rotate div {{ writing-mode: vertical-lr; transform: rotate(180deg); min-height: 60px; }}
  .base-label {{ font-weight: 600; text-align: left !important; position: sticky; left: 0;
                 background: var(--bg); z-index: 1; }}
  .has-pair {{ background: rgba(63,185,80,0.2); color: var(--green); font-size: 0.9rem; }}
  .no-pair {{ background: transparent; }}
  .legend {{ display: flex; gap: 20px; margin: 12px 0; font-size: 0.8rem; color: var(--text-dim); }}
  .legend span {{ display: flex; align-items: center; gap: 6px; }}
  .legend .dot {{ width: 12px; height: 12px; border-radius: 3px; }}
  .dot-fiat {{ background: var(--green); }}
  .dot-stable {{ background: var(--accent); }}
  .dot-crypto {{ background: var(--yellow); }}
</style>
</head>
<body>
<h1>Coinbase Advanced Trade &mdash; Available Trading Pairs</h1>
<p class="subtitle">Generated {now} from the List Products endpoint</p>

<div class="stats">
  <div class="stat-card"><div class="label">Total Products</div><div class="value">{total_products}</div></div>
  <div class="stat-card"><div class="label">Online &amp; Tradeable</div><div class="value" style="color:var(--green)">{total_online}</div></div>
  <div class="stat-card"><div class="label">Disabled / Offline</div><div class="value" style="color:var(--red)">{total_disabled}</div></div>
  <div class="stat-card"><div class="label">Unique Base Assets</div><div class="value">{unique_bases}</div></div>
  <div class="stat-card"><div class="label">Quote Currencies</div><div class="value">{unique_quotes}</div></div>
  <div class="stat-card"><div class="label">Crypto-to-Crypto Quotes</div><div class="value" style="color:var(--yellow)">{len(crypto_quotes)}</div></div>
</div>

<div class="section">
  <h2>Pairs by Quote Currency</h2>
  <div class="legend">
    <span><div class="dot dot-fiat"></div> Fiat</span>
    <span><div class="dot dot-stable"></div> Stablecoin</span>
    <span><div class="dot dot-crypto"></div> Crypto</span>
  </div>
  <div class="tab-bar">
    {"".join(tab_buttons)}
  </div>
  <div class="filter-bar">
    <input type="text" id="pairFilter" placeholder="Filter pairs (e.g. ALGO, SOL, BTC)..." oninput="filterPairs(this.value)">
  </div>
  {"".join(tab_contents)}
</div>

<div class="section">
  <h2>Pair Availability Matrix (top {len(top_bases)} bases &times; {len(all_quotes)} quotes)</h2>
  <p style="color:var(--text-dim);font-size:0.85rem;margin-bottom:10px">
    Shows which base assets can be traded against which quote currencies.
    &#10003; = direct pair exists on Coinbase.
  </p>
  <div class="matrix-wrap">
    <table class="matrix">
      <thead><tr>{matrix_header}</tr></thead>
      <tbody>{"".join(matrix_rows)}</tbody>
    </table>
  </div>
</div>

<script>
// Tab switching
document.querySelectorAll('.tab-btn').forEach(btn => {{
  btn.addEventListener('click', () => {{
    document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
    document.querySelectorAll('.tab-content').forEach(c => c.style.display = 'none');
    btn.classList.add('active');
    document.getElementById(btn.dataset.tab).style.display = 'block';
  }});
}});

// Pair filter
function filterPairs(query) {{
  const q = query.toLowerCase();
  document.querySelectorAll('.tab-content').forEach(tab => {{
    const rows = tab.querySelectorAll('tbody tr');
    rows.forEach(row => {{
      const text = row.textContent.toLowerCase();
      row.style.display = text.includes(q) ? '' : 'none';
    }});
  }});
  // Also filter matrix
  document.querySelectorAll('.matrix tbody tr').forEach(row => {{
    const base = row.querySelector('.base-label')?.textContent.toLowerCase() || '';
    row.style.display = base.includes(q) ? '' : 'none';
  }});
}}
</script>
</body>
</html>"""


def main():
    print("Fetching all products from Coinbase...")
    products = fetch_all_products()
    print(f"  Found {len(products)} products")

    html_content = build_html(products)

    out_path = Path(__file__).resolve().parent.parent / "coinbase_pairs_report.html"
    out_path.write_text(html_content, encoding="utf-8")
    print(f"Report written to {out_path}")


if __name__ == "__main__":
    main()
