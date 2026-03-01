"""
Shared SQL helper for multi-currency pair filtering.

All stats mixin methods and dashboard routes use this to build SQL WHERE
clauses that match pairs ending in one of several quote currencies
(e.g. ``UPPER(pair) LIKE '%-EUR' OR UPPER(pair) LIKE '%-USD'``).

The ``quote_currency`` parameter accepted everywhere is now
``str | list[str] | None``:
  - None          → no filtering
  - "EUR"         → single currency (backward compat)
  - ["EUR","USD"] → multi-currency OR clause
"""

from __future__ import annotations


def qc_where(
    currencies: str | list[str] | None,
    col: str = "pair",
) -> tuple[str, list[str]]:
    """Build a SQL WHERE fragment and params for quote-currency filtering.

    Returns (sql_fragment, params) where *sql_fragment* is either:
      - ``""``  (empty — no filtering when *currencies* is None/empty)
      - ``" AND (UPPER({col}) LIKE ? OR UPPER({col}) LIKE ?)"``  (one per currency)

    Usage::

        frag, params = qc_where(qc)
        conn.execute(base_sql + frag + " ORDER BY ts", [*base_params, *params])
    """
    if not currencies:
        return "", []

    if isinstance(currencies, str):
        currencies = [currencies]

    if len(currencies) == 1:
        return f" AND UPPER({col}) LIKE ?", [f"%-{currencies[0].upper()}"]

    clauses = [f"UPPER({col}) LIKE ?" for _ in currencies]
    params = [f"%-{c.upper()}" for c in currencies]
    return f" AND ({' OR '.join(clauses)})", params
