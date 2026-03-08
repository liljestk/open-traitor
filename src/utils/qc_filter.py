"""
Shared SQL helper for multi-currency pair filtering.

All stats mixin methods and dashboard routes use this to build SQL WHERE
clauses that match pairs ending in one of several quote currencies
(e.g. ``UPPER(pair) LIKE '%-EUR' OR UPPER(pair) LIKE '%-USD'``).

The ``quote_currency`` parameter accepted everywhere is now
``str | list[str] | None``:
  - None          -> no filtering
  - "EUR"         -> single currency (backward compat)
  - ["EUR","USD"] -> multi-currency OR clause
"""

from __future__ import annotations

# C9: Allowlist of valid column names to prevent SQL injection via col parameter
_VALID_COLUMNS = frozenset({"pair", "symbol", "trade_pair", "asset_pair"})
# Allow table-qualified references like "t.pair", "ar.pair"
_VALID_ALIASES = frozenset({"t", "ar", "tr", "s", "p"})


def _validate_col(col: str) -> None:
    """Validate column name (optionally table-qualified) against allowlist."""
    if col in _VALID_COLUMNS:
        return
    # Accept "alias.column" where alias ∈ _VALID_ALIASES and column ∈ _VALID_COLUMNS
    if "." in col:
        alias, _, bare = col.partition(".")
        if alias in _VALID_ALIASES and bare in _VALID_COLUMNS:
            return
    raise ValueError(
        f"Invalid column name {col!r} — must be one of {sorted(_VALID_COLUMNS)} "
        f"(optionally prefixed with a table alias: {sorted(_VALID_ALIASES)})"
    )


def qc_where(
    currencies: str | list[str] | None,
    col: str = "pair",
) -> tuple[str, list[str]]:
    """Build a SQL WHERE fragment and params for quote-currency filtering.

    Returns (sql_fragment, params) where *sql_fragment* is either:
      - ``""``  (empty -- no filtering when *currencies* is None/empty)
      - ``" AND (UPPER({col}) LIKE %s OR UPPER({col}) LIKE %s)"``  (one per currency)

    Usage::

        frag, params = qc_where(qc)
        conn.execute(base_sql + frag + " ORDER BY ts", [*base_params, *params])
    """
    # C9: Validate column name against allowlist to prevent SQL injection
    _validate_col(col)

    if not currencies:
        return "", []

    if isinstance(currencies, str):
        currencies = [currencies]

    if len(currencies) == 1:
        return f" AND UPPER({col}) LIKE %s", [f"%-{currencies[0].upper()}"]

    clauses = [f"UPPER({col}) LIKE %s" for _ in currencies]
    params = [f"%-{c.upper()}" for c in currencies]
    return f" AND ({' OR '.join(clauses)})", params
