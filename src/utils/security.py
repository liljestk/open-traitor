"""
Security utilities — Input sanitization, HMAC verification, credential handling.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import re
import secrets
import time
from typing import Optional

from src.utils.logger import get_logger

logger = get_logger("utils.security")

# Module-level constant: homoglyph translation table (computed once at import)
_HOMOGLYPH_TRANS = str.maketrans({
    '\u0430': 'a', '\u0435': 'e', '\u043e': 'o', '\u0440': 'p',
    '\u0441': 'c', '\u0443': 'y', '\u0445': 'x', '\u0456': 'i',
    '\u0458': 'j', '\u04bb': 'h', '\u0455': 's', '\u0457': 'i',
    '\u0410': 'A', '\u0412': 'B', '\u0415': 'E', '\u041a': 'K',
    '\u041c': 'M', '\u041d': 'H', '\u041e': 'O', '\u0420': 'P',
    '\u0421': 'C', '\u0422': 'T', '\u0425': 'X',
    # Greek
    '\u03b1': 'a', '\u03bf': 'o', '\u03b5': 'e', '\u03c1': 'p',
    '\u0391': 'A', '\u0392': 'B', '\u0395': 'E', '\u0397': 'H',
    '\u039a': 'K', '\u039c': 'M', '\u039d': 'N', '\u039f': 'O',
    '\u03a1': 'P', '\u03a4': 'T', '\u03a7': 'X', '\u0396': 'Z',
})


def sanitize_input(text: str, max_length: int = 500) -> str:
    """
    Sanitize user input from Telegram or any external source.
    Prevents injection attacks in prompts sent to the LLM.
    """
    if not text:
        return ""

    # Truncate
    text = text[:max_length]

    # Normalize Unicode FIRST (NFKC maps confusable glyphs to ASCII),
    # THEN strip invisible chars, so that homoglyph bypass is blocked before
    # the injection regex runs.
    import unicodedata
    text = unicodedata.normalize("NFKC", text)

    # M22 fix: Map Cyrillic / Greek visual homoglyphs to Latin *before*
    # injection regex runs.  NFKC does NOT handle cross-script lookalikes
    # (e.g. Cyrillic 'а' U+0430 looks like Latin 'a' but is distinct).
    text = text.translate(_HOMOGLYPH_TRANS)

    # Remove control characters (except newlines and tabs)
    text = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]', '', text)

    # Remove zero-width and invisible Unicode characters
    text = re.sub(r'[\u200b-\u200f\u2028-\u202f\u2060-\u206f\ufeff]', '', text)

    # Second normalization pass: catch any remaining confusables exposed
    # after invisible char removal (e.g. combining marks that were hidden
    # between zero-width chars).
    text = unicodedata.normalize("NFKC", text)

    # Remove potential prompt injection markers
    injection_patterns = [
        r'(?i)ignore\s+(all\s+)?previous\s+instructions',
        r'(?i)forget\s+(all\s+)?previous',
        r'(?i)you\s+are\s+now\s+',
        r'(?i)new\s+instructions?\s*:',
        r'(?i)system\s*:\s*',
        r'(?i)\[INST\]',
        r'(?i)<\|im_start\|>',
        r'(?i)<<SYS>>',
        r'(?i)<\|system\|>',
        r'(?i)<\|user\|>',
        r'(?i)<\|assistant\|>',
        r'(?i)###\s*(system|instruction|human|assistant)\s*:',
        r'(?i)act\s+as\s+(if\s+)?you\s+(are|were)',
        r'(?i)pretend\s+(you\s+are|to\s+be)',
        r'(?i)disregard\s+(all\s+)?(prior|above|previous)',
        r'(?i)override\s+(system|safety|instructions)',
        r'(?i)jailbreak',
        r'(?i)do\s+anything\s+now',
        r'(?i)DAN\s+mode',
    ]

    for pattern in injection_patterns:
        if re.search(pattern, text):
            logger.warning(f"⚠️ Potential prompt injection detected and sanitized")
            text = re.sub(pattern, '[FILTERED]', text)

    return text.strip()


def validate_trading_pair(pair: str) -> bool:
    """Validate a trading pair format (e.g., BTC-USD)."""
    return bool(re.match(r'^[A-Z0-9]{2,10}-[A-Z]{3,4}$', pair.upper()))


def validate_amount(amount: float, min_val: float = 0.01, max_val: float = 100000) -> bool:
    """Validate a trading amount is within sane bounds."""
    return min_val <= amount <= max_val


_ALLOWED_HMAC_ALGORITHMS = frozenset({"sha256", "sha512"})


def verify_hmac(
    message: str,
    signature: str,
    secret: str,
    algorithm: str = "sha256",
) -> bool:
    """Verify an HMAC signature — constant-time comparison."""
    if algorithm not in _ALLOWED_HMAC_ALGORITHMS:
        raise ValueError(
            f"Unsupported HMAC algorithm: {algorithm!r}. "
            f"Allowed: {sorted(_ALLOWED_HMAC_ALGORITHMS)}"
        )
    expected = hmac.new(
        secret.encode("utf-8"),
        message.encode("utf-8"),
        getattr(hashlib, algorithm),
    ).hexdigest()
    return hmac.compare_digest(expected, signature)


def mask_secret(secret: str, show_chars: int = 4) -> str:
    """Mask a secret string for safe logging."""
    if not secret or len(secret) <= show_chars * 2:
        return "***"
    return f"{secret[:show_chars]}...{secret[-show_chars:]}"


def validate_env_credentials() -> dict[str, bool]:
    """
    Validate that required environment variables are set.
    Returns a dict of credential_name -> is_set.
    """
    required = {
        "COINBASE_API_KEY": os.environ.get("COINBASE_API_KEY", ""),
        "COINBASE_API_SECRET": os.environ.get("COINBASE_API_SECRET", ""),
    }

    optional = {
        "TELEGRAM_BOT_TOKEN": os.environ.get("TELEGRAM_BOT_TOKEN", ""),
        "TELEGRAM_CHAT_ID": os.environ.get("TELEGRAM_CHAT_ID", ""),
        "REDDIT_CLIENT_ID": os.environ.get("REDDIT_CLIENT_ID", ""),
        "REDDIT_CLIENT_SECRET": os.environ.get("REDDIT_CLIENT_SECRET", ""),
    }

    status = {}

    for name, value in required.items():
        is_set = bool(value) and value not in ("your-key-here", "")
        status[name] = is_set
        if is_set:
            logger.debug(f"  ✅ {name}: {mask_secret(value)}")
        else:
            logger.warning(f"  ❌ {name}: NOT SET (required)")

    for name, value in optional.items():
        is_set = bool(value) and not value.startswith("your-")
        status[name] = is_set
        if is_set:
            logger.debug(f"  ✅ {name}: {mask_secret(value)}")
        else:
            logger.debug(f"  ⚠️ {name}: not set (optional)")

    return status


def generate_nonce() -> str:
    """Generate a cryptographic nonce for API requests."""
    return secrets.token_hex(16)


def constant_time_compare(a: str, b: str) -> bool:
    """Constant-time string comparison to prevent timing attacks."""
    return hmac.compare_digest(a.encode(), b.encode())
