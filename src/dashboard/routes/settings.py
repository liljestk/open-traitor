from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import tempfile
import threading
import time
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel as _BaseModel

import src.dashboard.deps as deps
from src.dashboard import auth
from src.utils.logger import get_logger
from src.utils.rpm_budget import compute_rpm_entity_cap

logger = get_logger("dashboard.settings")

router = APIRouter(tags=["Settings"])


# ═══════════════════════════════════════════════════════════════════════════
# Settings-manager imports
# ═══════════════════════════════════════════════════════════════════════════

from src.utils.settings_manager import (
    get_full_settings as _sm_get_full,
    get_schema_metadata as _sm_get_schema,
    update_section as _sm_update_section,
    apply_preset as _sm_apply_preset,
    push_to_runtime as _sm_push_runtime,
    push_section_to_runtime as _sm_push_section,
    PRESETS as _SM_PRESETS,
    get_preset_summary as _sm_preset_summary,
    is_trading_enabled as _sm_is_trading_enabled,
    is_telegram_allowed as _sm_tg_allowed,
    TELEGRAM_SAFETY_TIERS as _SM_TG_TIERS,
    get_llm_providers as _sm_get_providers,
    update_llm_providers as _sm_update_providers,
    STYLE_MODIFIER_META as _SM_STYLE_MODIFIERS,
    VALID_STYLE_MODIFIERS as _SM_VALID_MODIFIERS,
)


# ═══════════════════════════════════════════════════════════════════════════
# Local helpers / constants
# ═══════════════════════════════════════════════════════════════════════════

_CONFIRM_TTL_SECONDS = 120  # 2-minute window to confirm

# M7 fix: Rate limit confirmation token generation (max 10 per IP per 60s)
_confirmation_rate: dict[str, list[float]] = {}
_confirmation_rate_lock = threading.Lock()
_CONFIRM_RATE_LIMIT = 10
_CONFIRM_RATE_WINDOW = 60.0  # seconds


def _check_confirmation_rate(ip: str) -> bool:
    """Return True if the IP is within rate limits for confirmation token generation."""
    now = time.monotonic()
    with _confirmation_rate_lock:
        timestamps = _confirmation_rate.get(ip, [])
        # Prune old entries
        timestamps = [t for t in timestamps if now - t < _CONFIRM_RATE_WINDOW]
        if len(timestamps) >= _CONFIRM_RATE_LIMIT:
            _confirmation_rate[ip] = timestamps
            return False
        timestamps.append(now)
        _confirmation_rate[ip] = timestamps
        # Evict stale IPs to prevent unbounded growth
        if len(_confirmation_rate) > 1000:
            stale = [k for k, v in _confirmation_rate.items()
                     if not v or now - v[-1] > _CONFIRM_RATE_WINDOW]
            for k in stale:
                del _confirmation_rate[k]
        return True


def _prune_expired_confirmations() -> None:
    """Remove expired confirmation tokens."""
    deps.expire_confirmations()  # M24 fix: delegate to thread-safe helper


def _parse_env_file(path: str) -> dict[str, str]:
    """Parse a .env file into a dict, ignoring comments and blank lines."""
    result: dict[str, str] = {}
    if not os.path.exists(path):
        return result
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                continue
            idx = line.index("=")
            key = line[:idx].strip()
            value = line[idx + 1:].strip()
            result[key] = value
    return result


def _update_env_file(env_path: str, updates: dict[str, str]) -> None:
    """Update or append env vars in a .env file, preserving existing content.

    Uses atomic write (write to temp file → rename) to avoid partial writes
    corrupting the .env file on crash or power loss.
    """
    lines: list[str] = []
    if os.path.exists(env_path):
        with open(env_path, "r", encoding="utf-8") as f:
            lines = f.readlines()

    updated_keys: set[str] = set()
    new_lines: list[str] = []
    for line in lines:
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            key = stripped.split("=", 1)[0].strip()
            if key in updates:
                # Cycle-4 fix: strip newlines to prevent .env injection
                safe_val = str(updates[key]).replace("\n", "").replace("\r", "")
                new_lines.append(f"{key}={safe_val}\n")
                updated_keys.add(key)
                continue
        new_lines.append(line)

    # Append any keys that weren't already in the file
    remaining = set(updates.keys()) - updated_keys
    if remaining:
        if new_lines and not new_lines[-1].endswith("\n"):
            new_lines.append("\n")
        new_lines.append("\n# LLM Provider API Keys (added by dashboard)\n")
        for key in sorted(remaining):
            safe_val = str(updates[key]).replace("\n", "").replace("\r", "")
            new_lines.append(f"{key}={safe_val}\n")

    # Atomic write: write to temp file in same directory, then rename
    env_dir = os.path.dirname(os.path.abspath(env_path))
    fd, tmp_path = tempfile.mkstemp(dir=env_dir, suffix=".env.tmp", prefix=".env_")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.writelines(new_lines)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, os.path.abspath(env_path))
    except BaseException:
        # Clean up temp file on failure
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


# ═══════════════════════════════════════════════════════════════════════════
# Request/response models
# ═══════════════════════════════════════════════════════════════════════════

class _SetupConfigBody(_BaseModel):
    config_env: dict[str, str]  # env vars for config/.env
    root_env: dict[str, str]  # env vars for root .env (Docker Compose)
    assets: dict | None = None  # {coinbase_pairs: [...], ibkr_pairs: [...]}


class _SettingsUpdateBody(_BaseModel):
    section: Optional[str] = None
    updates: Optional[dict] = None
    preset: Optional[str] = None
    confirmation_token: Optional[str] = None  # Required for sensitive sections


class _ProvidersUpdateBody(_BaseModel):
    providers: list[dict]


class _ApiKeysUpdateBody(_BaseModel):
    keys: dict[str, str]  # env_var_name → value
    confirmation_token: Optional[str] = None  # Required on second step


# Sections that require confirmation before mutation
_SETTINGS_CONFIRM_SECTIONS = frozenset({
    "absolute_rules", "trading", "high_stakes",
})


# ═══════════════════════════════════════════════════════════════════════════
# Health check
# ═══════════════════════════════════════════════════════════════════════════

@router.get("/health", include_in_schema=False)
def health(request: Request):
    # Return minimal info when unauthenticated to avoid leaking service topology.
    authenticated = auth.is_authenticated(request)
    base = {"status": "ok", "ts": deps.utcnow()}
    if authenticated:
        base.update({
            "db": deps.stats_db is not None,
            "redis": deps.redis_client is not None,
            "temporal": deps.temporal_client is not None,
            "ws_clients": len(deps.ws_connections),
        })
    return base


# ═══════════════════════════════════════════════════════════════════════════
# Setup Wizard (initial configuration)
# ═══════════════════════════════════════════════════════════════════════════

@router.get("/api/setup", summary="Load current configuration for the setup wizard")
def get_setup_config():
    """Read config/.env, root .env, and YAML configs and return a
    WizardState-compatible JSON so the frontend wizard can pre-populate."""
    try:
        import yaml as _yaml

        config_env = _parse_env_file(os.path.join("config", ".env"))
        # Try config/root.env first (Docker), fall back to .env (host)
        root_env = _parse_env_file(os.path.join("config", "root.env"))
        if not root_env:
            root_env = _parse_env_file(".env")

        if not config_env:
            return {"exists": False}

        env = config_env.get

        # Detect active exchanges: YAML must exist AND exchange-specific
        # credentials / tokens must be configured in the env file.
        _EXCHANGE_CRED_KEYS: dict[str, list[str]] = {
            "coinbase": ["COINBASE_API_KEY"],
            "ibkr": ["TELEGRAM_BOT_TOKEN_IBKR", "IBKR_ACCOUNT"],
        }
        exchanges = {"coinbase": False, "ibkr": False}
        yaml_pairs: dict[str, list[str]] = {}
        exchange_currencies: dict[str, str] = {}
        for exch, fname in [("coinbase", "coinbase.yaml"), ("ibkr", "ibkr.yaml")]:
            ypath = os.path.join("config", fname)
            if not os.path.exists(ypath):
                continue
            # Check that at least one credential key is set for this exchange
            has_creds = any(
                config_env.get(k, "").strip()
                for k in _EXCHANGE_CRED_KEYS.get(exch, [])
            )
            exchanges[exch] = has_creds
            try:
                with open(ypath, "r", encoding="utf-8") as f:
                    ycfg = _yaml.safe_load(f) or {}
                yaml_pairs[exch] = (ycfg.get("trading") or {}).get("pairs", [])
                exchange_currencies[exch] = (ycfg.get("trading") or {}).get("quote_currency", "EUR")
            except Exception:
                yaml_pairs[exch] = []
        # ENV override: IBKR_CURRENCY takes precedence over YAML
        if config_env.get("IBKR_CURRENCY", "").strip():
            exchange_currencies["ibkr"] = config_env["IBKR_CURRENCY"].strip()

        # Map env vars → WizardState fields
        trading_mode = env("TRADING_MODE", "paper")
        live_confirmed = env("LIVE_TRADING_CONFIRMED", "") != ""

        # Telegram: parse authorized users
        authorized = env("TELEGRAM_AUTHORIZED_USERS", "")
        user_ids = [u.strip() for u in authorized.split(",") if u.strip()]
        primary_user = user_ids[0] if user_ids else ""
        additional_users = ",".join(user_ids[1:]) if len(user_ids) > 1 else ""
        telegram_enabled = bool(primary_user)

        # Infrastructure secrets — report presence only, never expose values (C3)
        infra_secrets = {}
        _INFRA_KEYS = [
            "REDIS_PASSWORD", "REDIS_URL",
            "TEMPORAL_DB_USER", "TEMPORAL_DB_PASSWORD", "TEMPORAL_DB_NAME",
            "LANGFUSE_DB_PASSWORD", "LANGFUSE_NEXTAUTH_SECRET", "LANGFUSE_SALT",
            "LANGFUSE_ADMIN_PASSWORD", "LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY",
            "CLICKHOUSE_PASSWORD", "MINIO_ROOT_USER", "MINIO_ROOT_PASSWORD",
            "LANGFUSE_ENCRYPTION_KEY",
        ]
        for k in _INFRA_KEYS:
            infra_secrets[k] = {"is_set": k in config_env and bool(config_env[k].strip())}

        # C3: Helper to report secret presence without exposing values
        def _mask(val: str) -> str:
            """Return masked representation: first 4 chars + '***' or empty."""
            if not val or not val.strip():
                return ""
            v = val.strip()
            if len(v) <= 4:
                return "****"
            return v[:4] + "****"

        state = {
            "exists": True,
            "exchanges": exchanges,
            "exchangeCurrencies": exchange_currencies,
            "tradingMode": trading_mode,
            "liveConfirmed": live_confirmed,
            "cryptoPairs": yaml_pairs.get("coinbase", []),
            "customCryptoPair": "",
            "ibkrPairs": yaml_pairs.get("ibkr", []),
            "customIbkrPair": "",
            "coinbaseApiKey": _mask(env("COINBASE_API_KEY", "")),
            "coinbaseApiKeySet": bool(env("COINBASE_API_KEY", "").strip()),
            "coinbaseApiSecretSet": bool(env("COINBASE_API_SECRET", "").strip()),
            "ibkrHost": env("IBKR_HOST", "127.0.0.1"),
            "ibkrPort": env("IBKR_PORT", "4002"),
            "ibkrClientId": env("IBKR_CLIENT_ID", "1"),
            "ibkrCurrency": env("IBKR_CURRENCY", "USD"),
            "geminiEnabled": env("GEMINI_API_KEY", "") != "",
            "geminiApiKey": _mask(env("GEMINI_API_KEY", "")),
            "openrouterEnabled": env("OPENROUTER_API_KEY", "") != "",
            "openrouterApiKey": _mask(env("OPENROUTER_API_KEY", "")),
            "openaiEnabled": env("OPENAI_API_KEY", "") != "",
            "openaiApiKey": _mask(env("OPENAI_API_KEY", "")),
            "groqEnabled": env("GROQ_API_KEY", "") != "",
            "groqApiKey": _mask(env("GROQ_API_KEY", "")),
            "ollamaModel": env("OLLAMA_MODEL", "qwen2.5:14b"),
            "telegramEnabled": telegram_enabled,
            "telegramUserId": primary_user,
            "telegramAdditionalUsers": additional_users,
            "telegramCoinbaseBotToken": _mask(env("TELEGRAM_BOT_TOKEN_COINBASE", "")),
            "telegramCoinbaseChatId": env("TELEGRAM_CHAT_ID_COINBASE", ""),
            "telegramIbkrBotToken": _mask(env("TELEGRAM_BOT_TOKEN_IBKR", "")),
            "telegramIbkrChatId": env("TELEGRAM_CHAT_ID_IBKR", ""),
            "redditEnabled": env("REDDIT_CLIENT_ID", "") != "",
            "redditClientId": _mask(env("REDDIT_CLIENT_ID", "")),
            "redditClientSecret": _mask(env("REDDIT_CLIENT_SECRET", "")),
            "redditUserAgent": env("REDDIT_USER_AGENT", "auto-traitor/1.0"),
            # Infra secrets: presence flags only (C3)
            "infraSecrets": infra_secrets,
        }
        return state
    except Exception as exc:
        logger.exception("setup GET error")
        raise HTTPException(status_code=500, detail="Internal server error")


@router.post("/api/setup", summary="Save initial configuration from setup wizard")
def setup_config(body: _SetupConfigBody, request: Request):
    """Write config/.env and root .env from the setup wizard.

    Also updates YAML config files with selected trading pairs if provided.
    """
    try:
        source_ip = request.client.host if request.client else "unknown"

        # C4: Allowlist of keys permitted in config/.env file writes
        _ALLOWED_CONFIG_ENV_KEYS = {
            "COINBASE_API_KEY", "COINBASE_API_SECRET", "COINBASE_KEY_FILE",
            "TRADING_MODE", "LIVE_TRADING_CONFIRMED", "PAPER_MODE",
            "REDIS_URL", "REDIS_PASSWORD",
            "OLLAMA_BASE_URL", "OLLAMA_MODEL",
            "GEMINI_API_KEY", "OPENROUTER_API_KEY", "OPENAI_API_KEY",
            "ANTHROPIC_API_KEY", "GROQ_API_KEY",
            "TELEGRAM_BOT_TOKEN_COINBASE", "TELEGRAM_CHAT_ID_COINBASE",
            "TELEGRAM_BOT_TOKEN_IBKR", "TELEGRAM_CHAT_ID_IBKR",
            "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID",
            "TELEGRAM_AUTHORIZED_USERS",
            "LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY", "LANGFUSE_HOST",
            "LANGFUSE_DB_PASSWORD", "LANGFUSE_NEXTAUTH_SECRET", "LANGFUSE_SALT",
            "LANGFUSE_ADMIN_PASSWORD", "LANGFUSE_ENCRYPTION_KEY",
            "TEMPORAL_HOST", "TEMPORAL_NAMESPACE",
            "TEMPORAL_DB_USER", "TEMPORAL_DB_PASSWORD", "TEMPORAL_DB_NAME",
            "CLICKHOUSE_PASSWORD", "MINIO_ROOT_USER", "MINIO_ROOT_PASSWORD",
            "DASHBOARD_API_KEY", "DASHBOARD_COMMAND_SIGNING_KEY",
            "LOG_LEVEL",
            "IBKR_HOST", "IBKR_PORT", "IBKR_CLIENT_ID", "IBKR_ACCOUNT",
            "IBKR_CURRENCY",
            "REDDIT_CLIENT_ID", "REDDIT_CLIENT_SECRET", "REDDIT_USER_AGENT",
        }
        _ALLOWED_ROOT_ENV_KEYS = {
            "REDIS_PASSWORD", "REDIS_URL",
            "TEMPORAL_DB_USER", "TEMPORAL_DB_PASSWORD", "TEMPORAL_DB_NAME",
            "LANGFUSE_DB_PASSWORD", "LANGFUSE_NEXTAUTH_SECRET", "LANGFUSE_SALT",
            "LANGFUSE_ADMIN_PASSWORD", "LANGFUSE_ENCRYPTION_KEY",
            "LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY",
            "CLICKHOUSE_PASSWORD", "MINIO_ROOT_USER", "MINIO_ROOT_PASSWORD",
            "DASHBOARD_API_KEY", "DASHBOARD_COMMAND_SIGNING_KEY",
        }

        # Filter env dicts through allowlists before writing (C4)
        filtered_config_env = {}
        for key, value in body.config_env.items():
            if key in _ALLOWED_CONFIG_ENV_KEYS:
                filtered_config_env[key] = value
            else:
                logger.warning(f"Setup wizard: rejected non-allowlisted config env key {key!r} (ip={source_ip})")
        filtered_root_env = {}
        for key, value in body.root_env.items():
            if key in _ALLOWED_ROOT_ENV_KEYS:
                filtered_root_env[key] = value
            else:
                logger.warning(f"Setup wizard: rejected non-allowlisted root env key {key!r} (ip={source_ip})")

        # 1. Write config/.env
        config_env_path = os.path.join("config", ".env")
        os.makedirs("config", exist_ok=True)

        # Backup existing config/.env if present
        if os.path.exists(config_env_path):
            backup_path = f"{config_env_path}.backup.{int(time.time())}"
            import shutil
            shutil.copy2(config_env_path, backup_path)
            logger.info(f"Backed up existing config/.env to {backup_path}")

        # Build file content with comments
        config_lines = [
            "# ===========================================",
            "# Auto-Traitor Environment Configuration",
            f"# Generated: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}",
            "# Generated by: Setup Wizard (web)",
            "# ===========================================",
            "",
        ]
        for key, value in filtered_config_env.items():
            config_lines.append(f"{key}={str(value).replace(chr(10), '').replace(chr(13), '')}")
        config_lines.append("")

        env_dir = os.path.dirname(os.path.abspath(config_env_path))
        fd, tmp_path = tempfile.mkstemp(dir=env_dir, suffix=".env.tmp", prefix=".env_")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write("\n".join(config_lines))
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, os.path.abspath(config_env_path))
        except BaseException:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise

        # 2. Write root .env (Docker Compose substitution vars)
        # Written to config/root.env because the container FS is read-only;
        # the config/ dir is a bind-mount so this is writable and visible on
        # the host as config/root.env (symlinked or copied to .env by the user
        # or docker-compose override).
        root_env_path = os.path.join("config", "root.env")
        root_lines = [
            "# Docker Compose variable substitution — generated by setup wizard, do not commit",
            "",
        ]
        for key, value in filtered_root_env.items():
            root_lines.append(f"{key}={str(value).replace(chr(10), '').replace(chr(13), '')}")
        root_lines.append("")

        root_dir = os.path.dirname(os.path.abspath(root_env_path)) or "."
        fd2, tmp2 = tempfile.mkstemp(dir=root_dir, suffix=".env.tmp", prefix=".env_")
        try:
            with os.fdopen(fd2, "w", encoding="utf-8") as f:
                f.write("\n".join(root_lines))
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp2, os.path.abspath(root_env_path))
        except BaseException:
            try:
                os.unlink(tmp2)
            except OSError:
                pass
            raise

        # Also try writing the actual root .env for non-containerized runs
        try:
            actual_root = ".env"
            actual_dir = os.path.dirname(os.path.abspath(actual_root)) or "."
            fd3, tmp3 = tempfile.mkstemp(dir=actual_dir, suffix=".env.tmp", prefix=".env_")
            try:
                with os.fdopen(fd3, "w", encoding="utf-8") as f:
                    f.write("\n".join(root_lines))
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(tmp3, os.path.abspath(actual_root))
            except BaseException:
                try:
                    os.unlink(tmp3)
                except OSError:
                    pass
                raise
        except OSError:
            # Expected in read-only container — config/root.env is the fallback
            logger.info("Skipped root .env write (read-only filesystem); wrote config/root.env instead")

        # 3. Update trading pairs in YAML configs if provided
        updated_yamls: list[str] = []
        if body.assets:
            import yaml

            coinbase_pairs = body.assets.get("coinbase_pairs")
            if coinbase_pairs and isinstance(coinbase_pairs, list):
                cb_path = os.path.join("config", "coinbase.yaml")
                if os.path.exists(cb_path):
                    with open(cb_path, "r", encoding="utf-8") as f:
                        cb_cfg = yaml.safe_load(f) or {}
                    if "trading" in cb_cfg:
                        cb_cfg["trading"]["pairs"] = coinbase_pairs
                    with open(cb_path, "w", encoding="utf-8") as f:
                        yaml.dump(cb_cfg, f, default_flow_style=False, allow_unicode=True)
                    updated_yamls.append("coinbase.yaml")

            ibkr_pairs = body.assets.get("ibkr_pairs")
            if ibkr_pairs and isinstance(ibkr_pairs, list):
                ib_path = os.path.join("config", "ibkr.yaml")
                if os.path.exists(ib_path):
                    with open(ib_path, "r", encoding="utf-8") as f:
                        ib_cfg = yaml.safe_load(f) or {}
                    if "trading" in ib_cfg:
                        ib_cfg["trading"]["pairs"] = ibkr_pairs
                    with open(ib_path, "w", encoding="utf-8") as f:
                        yaml.dump(ib_cfg, f, default_flow_style=False, allow_unicode=True)
                    updated_yamls.append("ibkr.yaml")

        # 4. Create data directories
        for d in ["data", "data/trades", "data/news", "data/journal", "data/audit", "logs"]:
            os.makedirs(d, exist_ok=True)

        # 5. Update os.environ with new values (allowlisted keys only)
        _ALLOWED_ENV_KEYS = {
            "COINBASE_API_KEY", "COINBASE_API_SECRET", "COINBASE_KEY_FILE",
            "REDIS_URL", "OLLAMA_BASE_URL", "OLLAMA_MODEL",
            "GEMINI_API_KEY", "OPENROUTER_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GROQ_API_KEY",
            "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "TELEGRAM_AUTHORIZED_USERS",
            "LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY", "LANGFUSE_HOST",
            "TEMPORAL_HOST", "TEMPORAL_NAMESPACE",
            "DASHBOARD_API_KEY", "DASHBOARD_COMMAND_SIGNING_KEY",
            "LOG_LEVEL", "PAPER_MODE",
            "IBKR_HOST", "IBKR_PORT", "IBKR_CLIENT_ID", "IBKR_CURRENCY",
        }
        for key, value in filtered_config_env.items():
            if key in _ALLOWED_ENV_KEYS:
                os.environ[key] = value
            else:
                logger.warning(f"Setup wizard: rejected unknown env key {key!r}")

        # 6. Hot-reload LLM providers so new API keys take effect immediately
        llm_reloaded = False
        if deps.llm_client:
            try:
                from src.core.llm_client import build_providers
                saved_providers = _sm_get_providers()
                llm_config = deps.get_config().get("llm", {})
                ollama_url = os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434")
                fallback_model = os.environ.get("OLLAMA_MODEL", llm_config.get("model", "llama3.1:8b"))
                new_providers = build_providers(
                    saved_providers,
                    fallback_base_url=ollama_url,
                    fallback_model=fallback_model,
                    fallback_timeout=llm_config.get("timeout", 60),
                    fallback_max_retries=llm_config.get("max_retries", 3),
                )
                deps.llm_client.reload_providers(new_providers)
                # Update stored config so recovery polling picks up new keys
                deps.llm_client.update_providers_config(
                    saved_providers,
                    fallback_base_url=ollama_url,
                    fallback_model=fallback_model,
                    fallback_timeout=llm_config.get("timeout", 60),
                    fallback_max_retries=llm_config.get("max_retries", 3),
                )
                llm_reloaded = True
            except Exception as _reload_err:
                logger.warning(f"LLM provider hot-reload after setup failed: {_reload_err}")

        logger.warning(
            f"⚙️ Setup wizard config saved: {len(body.config_env)} env vars, "
            f"yamls={updated_yamls}, llm_reloaded={llm_reloaded} (ip={source_ip})"
        )

        return {
            "ok": True,
            "config_env_path": config_env_path,
            "root_env_path": root_env_path,
            "env_vars_count": len(body.config_env),
            "updated_yamls": updated_yamls,
            "llm_reloaded": llm_reloaded,
        }
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("setup POST error")
        raise HTTPException(status_code=500, detail="Internal server error")


# ═══════════════════════════════════════════════════════════════════════════
# Settings (read, update, presets)
# ═══════════════════════════════════════════════════════════════════════════

@router.get("/api/settings", summary="Get all settings with metadata")
def get_settings():
    """Returns the full settings.yaml content, schema metadata, and presets info."""
    try:
        full = _sm_get_full()
        full["schema"] = _sm_get_schema()

        # Attach RPM budget breakdown so frontend can show limits
        try:
            cfg = deps.get_config()
            providers = cfg.get("llm_providers", [])
            interval = cfg.get("trading", {}).get("interval", 120)
            max_entities, breakdown = compute_rpm_entity_cap(providers, interval)
            configured_max = cfg.get("trading", {}).get("max_active_pairs", 5)
            full["rpm_budget"] = {
                **breakdown,
                "configured_max": configured_max,
                "effective_max": min(configured_max, max_entities),
            }
        except Exception as _rpm_err:
            logger.debug(f"rpm_budget enrichment skipped: {_rpm_err}")

        return full
    except Exception as exc:
        logger.exception("settings GET error")
        raise HTTPException(status_code=500, detail="Internal server error")


@router.put("/api/settings", summary="Update settings section or apply preset")
def update_settings(body: _SettingsUpdateBody, request: Request):
    """
    Two modes:
      1. ``{ "preset": "moderate" }`` — apply a named preset
      2. ``{ "section": "risk", "updates": {"stop_loss_pct": 0.05} }`` — update individual fields

    Sensitive sections (absolute_rules, trading, high_stakes) require a
    two-step confirmation flow — first call returns a ``confirmation_token``,
    second call with that token applies the change.

    All mutations are audit-logged.
    """
    try:
        _prune_expired_confirmations()
        source_ip = request.client.host if request.client else "unknown"

        # Mode 1: Apply preset
        if body.preset:
            # Presets always require confirmation
            if not body.confirmation_token:
                if not _check_confirmation_rate(source_ip):
                    raise HTTPException(status_code=429, detail="Too many confirmation requests")
                token = secrets.token_urlsafe(32)
                deps.store_confirmation(token, {
                    "action": "settings-preset",
                    "preset": body.preset,
                    "expires": time.monotonic() + _CONFIRM_TTL_SECONDS,
                })
                return {
                    "ok": False,
                    "confirmation_required": True,
                    "confirmation_token": token,
                    "message": f"Confirm applying preset '{body.preset}'.",
                    "expires_in_seconds": _CONFIRM_TTL_SECONDS,
                }

            pending = deps.pop_confirmation(body.confirmation_token)
            if not pending or pending["expires"] < time.monotonic():
                raise HTTPException(status_code=403, detail="Invalid or expired confirmation token")
            if pending.get("preset") != body.preset:
                raise HTTPException(status_code=400, detail="Preset does not match confirmation")

            ok, err, changes = _sm_apply_preset(body.preset)
            if not ok:
                raise HTTPException(status_code=400, detail=err)
            _sm_push_runtime(deps.rules_instance, deps.config, changes)
            logger.warning(
                f"⚙️ Settings preset applied: {body.preset} "
                f"({len(changes)} changes, ip={source_ip})"
            )
            return {
                "ok": True,
                "preset": body.preset,
                "changes": changes,
                "trading_enabled": _sm_is_trading_enabled(),
            }

        # Mode 2: Section update
        if not body.section or not body.updates:
            raise HTTPException(
                status_code=400,
                detail="Provide either {preset} or {section, updates}",
            )

        # Require confirmation for sensitive sections
        needs_confirm = body.section in _SETTINGS_CONFIRM_SECTIONS
        if needs_confirm and not body.confirmation_token:
            if not _check_confirmation_rate(source_ip):
                raise HTTPException(status_code=429, detail="Too many confirmation requests")
            token = secrets.token_urlsafe(32)
            # H10 fix: store updates hash so values can't be swapped on confirmation
            import hashlib as _hl
            _updates_hash = _hl.sha256(json.dumps(body.updates, sort_keys=True).encode()).hexdigest()
            deps.store_confirmation(token, {
                "action": "settings-section",
                "section": body.section,
                "field_names": sorted(body.updates.keys()),
                "updates_hash": _updates_hash,
                "expires": time.monotonic() + _CONFIRM_TTL_SECONDS,
            })
            return {
                "ok": False,
                "confirmation_required": True,
                "confirmation_token": token,
                "section": body.section,
                "fields_to_update": sorted(body.updates.keys()),
                "message": f"Confirm update to '{body.section}' settings.",
                "expires_in_seconds": _CONFIRM_TTL_SECONDS,
            }

        if needs_confirm:
            pending = deps.pop_confirmation(body.confirmation_token)
            if not pending or pending["expires"] < time.monotonic():
                raise HTTPException(status_code=403, detail="Invalid or expired confirmation token")
            if pending.get("section") != body.section:
                raise HTTPException(status_code=400, detail="Section does not match confirmation")
            # H10: verify the updates payload hasn't been swapped since confirmation was issued
            import hashlib as _hl
            current_hash = _hl.sha256(json.dumps(body.updates, sort_keys=True).encode()).hexdigest()
            if pending.get("updates_hash") and pending["updates_hash"] != current_hash:
                raise HTTPException(status_code=400, detail="Updates payload changed since confirmation was issued")

        ok, err, changes = _sm_update_section(body.section, body.updates)
        if not ok:
            raise HTTPException(status_code=400, detail=err)

        # Use deps.config (== orch.config, the live dict) not deps.get_config()
        # which returns a throwaway dict loaded from disk.
        _sm_push_section(body.section, changes, deps.rules_instance, deps.config)
        logger.warning(
            f"⚙️ Settings updated: section={body.section}, "
            f"fields={sorted(changes.keys()) if isinstance(changes, dict) else changes} "
            f"(ip={source_ip})"
        )
        return {
            "ok": True,
            "section": body.section,
            "changes": changes,
            "trading_enabled": _sm_is_trading_enabled(),
        }
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("settings PUT error")
        raise HTTPException(status_code=500, detail="Internal server error")


@router.get("/api/settings/presets", summary="List available presets")
def get_presets():
    """Returns all available presets with their values and human descriptions."""
    result = {}
    for name in _SM_PRESETS:
        result[name] = {
            "values": _SM_PRESETS[name],
            "summary": _sm_preset_summary(name),
        }
    return {"presets": result, "current_enabled": _sm_is_trading_enabled()}


@router.get("/api/settings/style-modifiers", summary="List available style modifiers")
def get_style_modifiers():
    """Returns all style modifiers with metadata and which are currently active."""
    try:
        cfg = deps.get_config()
        active = cfg.get("trading", {}).get("style_modifiers", [])
        exchange = cfg.get("trading", {}).get("exchange", "coinbase")
        asset_class = "equity" if exchange == "ibkr" else "crypto"
        return {
            "modifiers": _SM_STYLE_MODIFIERS,
            "active": active,
            "asset_class": asset_class,
        }
    except Exception:
        logger.exception("style-modifiers GET error")
        raise HTTPException(status_code=500, detail="Internal server error")


@router.get("/api/settings/telegram-tiers", summary="Telegram safety tier plan")
def get_telegram_tiers():
    """Returns which settings sections are safe/semi-safe/blocked for Telegram."""
    return _SM_TG_TIERS


# ═══════════════════════════════════════════════════════════════════════════
# LLM Provider management
# ═══════════════════════════════════════════════════════════════════════════

@router.get("/api/settings/llm-providers", summary="Get LLM provider chain with live status")
def get_llm_providers():
    """
    Returns the configured LLM providers with their live status
    (daily tokens used, cooldown state, API key availability).
    """
    try:
        providers_config = _sm_get_providers()

        # Enrich with live status from LLMClient if available
        live_status = {}
        if deps.llm_client:
            for ps in deps.llm_client.provider_status():
                live_status[ps["name"]] = ps

        # All non-secret config fields (actual API key values are never exposed)
        _SAFE_PROVIDER_FIELDS = {
            "name", "model", "is_local", "enabled", "priority",
            "base_url", "base_url_env", "api_key_env", "model_env",
            "rpm_limit", "daily_token_limit", "daily_request_limit",
            "cooldown_seconds", "tier", "timeout",
            "reserve_for_priority",
        }
        # Import env resolver to check config/.env as well as os.environ
        try:
            from src.core.llm_providers import _resolve_env as _llm_resolve_env
        except Exception:
            _llm_resolve_env = lambda var, default="": os.environ.get(var, default)  # noqa: E731

        result = []
        for pc in providers_config:
            name = pc.get("name", "")
            entry = {k: v for k, v in pc.items() if k in _SAFE_PROVIDER_FIELDS}
            # Add live status if available
            if name in live_status:
                entry["live_status"] = live_status[name]
            # Indicate whether the API key is set (check both os.environ and config/.env)
            api_key_env = pc.get("api_key_env", "")
            if api_key_env:
                entry["api_key_set"] = bool(_llm_resolve_env(api_key_env))
            else:
                entry["api_key_set"] = pc.get("is_local", False)
            result.append(entry)

        return {"providers": result}
    except Exception as exc:
        logger.exception("llm-providers GET error")
        raise HTTPException(status_code=500, detail="Internal server error")


@router.get("/api/settings/openrouter-credits", summary="Check OpenRouter free-tier credits")
async def get_openrouter_credits():
    """Return OpenRouter credit balance and usage info.

    Calls the OpenRouter /api/v1/auth/key endpoint to check remaining credits,
    usage, and whether the key is on a free tier.
    """
    api_key = os.environ.get("OPENROUTER_API_KEY", "")
    if not api_key:
        return {"ok": False, "error": "OPENROUTER_API_KEY not set"}

    from src.core.llm_client import check_openrouter_credits
    info = await check_openrouter_credits(api_key)
    return info


@router.put("/api/settings/llm-providers", summary="Update LLM provider chain")
def update_llm_providers(body: _ProvidersUpdateBody):
    """
    Accepts a full ordered providers list. Validates, persists to settings.yaml,
    and hot-reloads the LLMClient's provider chain.
    """
    # Strip runtime-only fields that should never be persisted to YAML
    _RUNTIME_ONLY = {"api_key_set", "live_status"}
    clean_providers = [
        {k: v for k, v in p.items() if k not in _RUNTIME_ONLY}
        for p in body.providers
    ]
    try:
        ok, err, saved = _sm_update_providers(clean_providers)
        if not ok:
            raise HTTPException(status_code=400, detail=err)

        # Hot-reload the LLMClient if available
        if deps.llm_client:
            from src.core.llm_client import build_providers
            llm_config = deps.get_config().get("llm", {})
            ollama_url = os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434")
            fallback_model = os.environ.get("OLLAMA_MODEL", llm_config.get("model", "llama3.1:8b"))
            new_providers = build_providers(
                saved,
                fallback_base_url=ollama_url,
                fallback_model=fallback_model,
                fallback_timeout=llm_config.get("timeout", 60),
                fallback_max_retries=llm_config.get("max_retries", 3),
            )
            deps.llm_client.reload_providers(new_providers)
            deps.llm_client.update_providers_config(
                saved,
                fallback_base_url=ollama_url,
                fallback_model=fallback_model,
                fallback_timeout=llm_config.get("timeout", 60),
                fallback_max_retries=llm_config.get("max_retries", 3),
            )

        return {"ok": True, "providers": saved}
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("llm-providers PUT error")
        raise HTTPException(status_code=500, detail="Internal server error")


@router.put("/api/settings/api-keys", summary="Update API keys for LLM providers")
def update_api_keys(body: _ApiKeysUpdateBody, request: Request):
    """
    Two-step confirmation flow for credential updates:

    **Step 1** — Send ``{"keys": {"GEMINI_API_KEY": "AIza..."}}``
      Returns a ``confirmation_token`` and lists the keys that will be updated.

    **Step 2** — Re-send with the token: ``{"keys": {...}, "confirmation_token": "..."}``
      Validates, persists to config/.env (atomic write), and hot-reloads providers.
    """
    try:
        _prune_expired_confirmations()
        source_ip = request.client.host if request.client else "unknown"

        # Validate: only allow env vars referenced by providers' api_key_env
        providers_config = _sm_get_providers()
        allowed_vars: set[str] = set()
        for pc in providers_config:
            env_var = pc.get("api_key_env", "")
            if env_var:
                allowed_vars.add(env_var)

        for var_name in body.keys:
            if var_name not in allowed_vars:
                raise HTTPException(
                    status_code=400,
                    detail=f"'{var_name}' is not a recognized LLM provider API key env var. "
                           f"Allowed: {sorted(allowed_vars)}",
                )

        key_names = sorted(body.keys.keys())

        # ── Step 1: issue confirmation token ──────────────────────────
        if not body.confirmation_token:
            if not _check_confirmation_rate(source_ip):
                raise HTTPException(status_code=429, detail="Too many confirmation requests")
            token = secrets.token_urlsafe(32)
            deps.store_confirmation(token, {
                "action": "api-keys",
                "key_names": key_names,
                "expires": time.monotonic() + _CONFIRM_TTL_SECONDS,
            })
            logger.info(f"🔑 API key update requested (awaiting confirmation): {key_names}")
            return {
                "ok": False,
                "confirmation_required": True,
                "confirmation_token": token,
                "keys_to_update": key_names,
                "message": f"Confirm update of {len(key_names)} API key(s) by re-sending with confirmation_token.",
                "expires_in_seconds": _CONFIRM_TTL_SECONDS,
            }

        # ── Step 2: validate confirmation token ──────────────────────
        pending = deps.pop_confirmation(body.confirmation_token)
        if not pending:
            raise HTTPException(status_code=403, detail="Invalid or expired confirmation token")
        if pending["expires"] < time.monotonic():
            raise HTTPException(status_code=403, detail="Confirmation token expired")
        if sorted(pending["key_names"]) != key_names:
            raise HTTPException(
                status_code=400,
                detail="Key names do not match the original confirmation request",
            )

        # Update os.environ immediately
        for var_name, value in body.keys.items():
            os.environ[var_name] = value

        # Persist to config/.env (atomic write)
        env_path = os.path.join("config", ".env")
        _update_env_file(env_path, body.keys)

        # Hot-reload LLMClient providers so new keys take effect
        if deps.llm_client:
            from src.core.llm_client import build_providers
            saved_providers = _sm_get_providers()
            llm_config = deps.get_config().get("llm", {})
            ollama_url = os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434")
            fallback_model = os.environ.get("OLLAMA_MODEL", llm_config.get("model", "llama3.1:8b"))
            new_providers = build_providers(
                saved_providers,
                fallback_base_url=ollama_url,
                fallback_model=fallback_model,
                fallback_timeout=llm_config.get("timeout", 60),
                fallback_max_retries=llm_config.get("max_retries", 3),
            )
            deps.llm_client.reload_providers(new_providers)
            deps.llm_client.update_providers_config(
                saved_providers,
                fallback_base_url=ollama_url,
                fallback_model=fallback_model,
                fallback_timeout=llm_config.get("timeout", 60),
                fallback_max_retries=llm_config.get("max_retries", 3),
            )

        logger.warning(f"🔑 API keys updated (confirmed): {key_names}")
        return {"ok": True, "updated": key_names}
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("api-keys PUT error")
        raise HTTPException(status_code=500, detail="Internal server error")
