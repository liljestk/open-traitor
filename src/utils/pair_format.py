"""
Pair-format utility – normalises pair strings across exchanges.

Crypto pairs use "BASE-QUOTE"  (e.g. BTC-EUR, ETH-USD).
Equity pairs use ticker symbols without a separator (e.g. VOLV-B, ERIC-B).
Some exchanges use "/" or ":" as separator.

This module provides helpers to parse, validate, and format pairs
regardless of the exchange they come from.
"""

from __future__ import annotations

import re
from typing import Tuple

# Crypto quote currencies that signal a crypto pair
_CRYPTO_QUOTES = frozenset({
    "USD", "EUR", "GBP", "USDT", "USDC", "BTC", "ETH", "DAI",
})

# Regex: two alpha-numeric segments separated by one of - / :
_PAIR_RE = re.compile(r"^([A-Z0-9]{2,10})[-/: ]([A-Z0-9]{2,10})$", re.IGNORECASE)


def parse_pair(pair: str) -> Tuple[str, str]:
    """
    Split a pair string into (base, quote).

    Supports separators: ``-``, ``/``, ``:``, and single space.

    Returns uppercase (base, quote).
    Raises ``ValueError`` if the pair cannot be parsed.

    >>> parse_pair("BTC-EUR")
    ('BTC', 'EUR')
    >>> parse_pair("eth/usdt")
    ('ETH', 'USDT')
    """
    m = _PAIR_RE.match(pair.strip())
    if not m:
        raise ValueError(f"Cannot parse pair: {pair!r}")
    return m.group(1).upper(), m.group(2).upper()


def is_crypto_pair(pair: str) -> bool:
    """
    Heuristic: returns *True* if the pair looks like a crypto pair
    (i.e. the quote currency is a known crypto quote currency).

    >>> is_crypto_pair("BTC-EUR")
    True
    >>> is_crypto_pair("VOLV-B")
    False
    """
    try:
        _base, quote = parse_pair(pair)
    except ValueError:
        return False
    return quote in _CRYPTO_QUOTES


def format_display_pair(pair: str, separator: str = "-") -> str:
    """
    Normalise a pair to ``BASE<sep>QUOTE`` (uppercase).

    >>> format_display_pair("eth/usd")
    'ETH-USD'
    >>> format_display_pair("VOLV-B", separator="/")
    'VOLV/B'
    """
    base, quote = parse_pair(pair)
    return f"{base}{separator}{quote}"
