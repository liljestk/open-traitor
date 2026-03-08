#!/usr/bin/env python3
"""
Automated data migration tool for Auto-Traitor.

Supports:
  1) on-prem → cloud (full export / import)
  2) container rebuild safety (Redis key migration)
  3) PostgreSQL dump / restore
  4) File-based data backup

Usage:
    # Full export to a directory
    python scripts/migrate_data.py export --out /path/to/backup

    # Import from a backup directory
    python scripts/migrate_data.py import --from /path/to/backup

    # Migrate Redis keys from old (unprefixed) → new (profile-prefixed) format
    python scripts/migrate_data.py redis-migrate --profile coinbase

    # Dry-run (preview what would happen)
    python scripts/migrate_data.py export --out /tmp/backup --dry-run

Prerequisites:
    - pg_dump / pg_restore available (or psycopg2 for logical export)
    - Redis accessible
    - DATABASE_URL set (or config/.env loaded)
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

# ---------------------------------------------------------------------------
# Env / DSN resolution
# ---------------------------------------------------------------------------

def _load_env() -> dict[str, str]:
    """Load environment from config/.env or config/root.env."""
    env = dict(os.environ)
    for env_file in ("config/.env", "config/root.env"):
        if os.path.exists(env_file):
            with open(env_file) as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith("#") and "=" in line:
                        k, v = line.split("=", 1)
                        v = v.strip().strip('"').strip("'")
                        env.setdefault(k.strip(), v)
    return env


def _get_dsn(env: dict[str, str]) -> str:
    dsn = env.get("DATABASE_URL", "")
    if not dsn:
        raise RuntimeError("DATABASE_URL not found in environment or config/.env")
    return dsn


def _get_redis_url(env: dict[str, str]) -> tuple[str, int, str]:
    """Return (host, port, password) for Redis."""
    host = env.get("REDIS_HOST", "127.0.0.1")
    port = int(env.get("REDIS_PORT", "6380"))
    password = env.get("REDIS_PASSWORD", "")
    return host, port, password


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------

def cmd_export(args: argparse.Namespace) -> None:
    """Export all data to a backup directory."""
    env = _load_env()
    out_dir = Path(args.out)
    dry = args.dry_run
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")

    if not dry:
        out_dir.mkdir(parents=True, exist_ok=True)

    manifest: dict = {
        "created_at": ts,
        "version": "1.0",
        "components": [],
    }

    # ── 1. PostgreSQL (trading DB) ──────────────────────────────────────
    dsn = _get_dsn(env)
    pg_file = out_dir / "traitor_db.sql.gz"
    print(f"[1/4] PostgreSQL trading DB → {pg_file}")
    if not dry:
        _pg_dump(dsn, pg_file)
    manifest["components"].append({"type": "postgres", "name": "traitor_db", "file": pg_file.name})

    # ── 2. Langfuse DB (if reachable) ────────────────────────────────────
    langfuse_dsn = env.get("LANGFUSE_DATABASE_URL", "")
    if not langfuse_dsn:
        # Build from compose defaults
        lf_pw = env.get("LANGFUSE_DB_PASSWORD", "")
        lf_host = env.get("LANGFUSE_DB_HOST", "127.0.0.1")
        if lf_pw:
            langfuse_dsn = f"postgresql://langfuse:{lf_pw}@{lf_host}:5432/langfuse"
    if langfuse_dsn:
        lf_file = out_dir / "langfuse_db.sql.gz"
        print(f"[2/4] Langfuse DB → {lf_file}")
        if not dry:
            try:
                _pg_dump(langfuse_dsn, lf_file)
                manifest["components"].append({"type": "postgres", "name": "langfuse_db", "file": lf_file.name})
            except Exception as e:
                print(f"      ⚠ Langfuse DB export skipped: {e}")
    else:
        print("[2/4] Langfuse DB → skipped (no DSN)")

    # ── 3. Redis snapshot ────────────────────────────────────────────────
    redis_file = out_dir / "redis_state.json"
    print(f"[3/4] Redis state → {redis_file}")
    if not dry:
        try:
            _export_redis(env, redis_file)
            manifest["components"].append({"type": "redis", "file": redis_file.name})
        except Exception as e:
            print(f"      ⚠ Redis export skipped: {e}")

    # ── 4. File data (configs, journals, audit) ──────────────────────────
    file_dirs = ["config", "data"]
    for d in file_dirs:
        src = Path(d)
        if not src.exists():
            continue
        dst = out_dir / "files" / d
        print(f"[4/4] {src}/ → {dst}/")
        if not dry:
            if dst.exists():
                shutil.rmtree(dst)
            shutil.copytree(
                src, dst,
                ignore=shutil.ignore_patterns(
                    "__pycache__", "*.pyc", ".env",  # never copy secrets
                ),
            )
            manifest["components"].append({"type": "files", "name": d, "path": f"files/{d}"})

    # Write manifest
    manifest_path = out_dir / "manifest.json"
    if not dry:
        with open(manifest_path, "w") as f:
            json.dump(manifest, f, indent=2)

    print(f"\n✅ Export {'(DRY RUN) ' if dry else ''}complete → {out_dir}")
    if not dry:
        # Verify file sizes
        total = sum(f.stat().st_size for f in out_dir.rglob("*") if f.is_file())
        print(f"   Total size: {total / 1024 / 1024:.1f} MB")


def _pg_dump(dsn: str, out_path: Path) -> None:
    """Run pg_dump with gzip compression."""
    parsed = urlparse(dsn)
    pg_env = dict(os.environ)
    pg_env["PGPASSWORD"] = parsed.password or ""
    host = parsed.hostname or "127.0.0.1"
    port = str(parsed.port or 5432)
    user = parsed.username or "traitor"
    dbname = parsed.path.lstrip("/")

    cmd = [
        "pg_dump",
        "-h", host, "-p", port, "-U", user,
        "--format=custom",  # binary format, supports pg_restore
        "--compress=6",
        "-f", str(out_path),
        dbname,
    ]
    result = subprocess.run(cmd, env=pg_env, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"pg_dump failed: {result.stderr.strip()}")
    print(f"      {out_path.stat().st_size / 1024:.0f} KB")


def _export_redis(env: dict[str, str], out_path: Path) -> None:
    """Export all non-volatile Redis keys to JSON."""
    import redis as _redis

    host, port, password = _get_redis_url(env)
    r = _redis.Redis(host=host, port=port, password=password, decode_responses=True)
    r.ping()

    data: dict[str, dict] = {}
    for key in r.scan_iter("*", count=500):
        key_type = r.type(key)
        ttl = r.ttl(key)
        if key_type == "string":
            data[key] = {"type": "string", "value": r.get(key), "ttl": ttl}
        elif key_type == "list":
            data[key] = {"type": "list", "value": r.lrange(key, 0, -1), "ttl": ttl}
        elif key_type == "hash":
            data[key] = {"type": "hash", "value": r.hgetall(key), "ttl": ttl}
        elif key_type == "set":
            data[key] = {"type": "set", "value": list(r.smembers(key)), "ttl": ttl}

    with open(out_path, "w") as f:
        json.dump(data, f, indent=2, default=str)
    print(f"      {len(data)} keys exported")


# ---------------------------------------------------------------------------
# Import
# ---------------------------------------------------------------------------

def cmd_import(args: argparse.Namespace) -> None:
    """Import data from a backup directory."""
    env = _load_env()
    src_dir = Path(getattr(args, "from"))
    dry = args.dry_run

    manifest_path = src_dir / "manifest.json"
    if not manifest_path.exists():
        print(f"❌ No manifest.json found in {src_dir}")
        sys.exit(1)

    with open(manifest_path) as f:
        manifest = json.load(f)

    print(f"Backup from: {manifest.get('created_at', 'unknown')}")
    print(f"Components: {len(manifest.get('components', []))}")
    print()

    for comp in manifest.get("components", []):
        ctype = comp["type"]

        if ctype == "postgres":
            name = comp["name"]
            dump_file = src_dir / comp["file"]
            if name == "traitor_db":
                dsn = _get_dsn(env)
            elif name == "langfuse_db":
                lf_pw = env.get("LANGFUSE_DB_PASSWORD", "")
                lf_host = env.get("LANGFUSE_DB_HOST", "127.0.0.1")
                dsn = f"postgresql://langfuse:{lf_pw}@{lf_host}:5432/langfuse"
            else:
                print(f"  ⚠ Unknown DB: {name} — skipping")
                continue
            print(f"  PostgreSQL [{name}] ← {dump_file}")
            if not dry:
                _pg_restore(dsn, dump_file)

        elif ctype == "redis":
            redis_file = src_dir / comp["file"]
            print(f"  Redis ← {redis_file}")
            if not dry:
                _import_redis(env, redis_file)

        elif ctype == "files":
            src_path = src_dir / comp["path"]
            dst_path = Path(comp["name"])
            print(f"  Files: {src_path} → {dst_path}/")
            if not dry:
                if dst_path.exists():
                    # Merge, don't overwrite — preserve .env and secrets
                    for item in src_path.rglob("*"):
                        if item.is_file():
                            rel = item.relative_to(src_path)
                            target = dst_path / rel
                            if target.name == ".env":
                                print(f"    ⚠ Skipping {target} (secrets file)")
                                continue
                            target.parent.mkdir(parents=True, exist_ok=True)
                            shutil.copy2(item, target)

    print(f"\n✅ Import {'(DRY RUN) ' if dry else ''}complete")


def _pg_restore(dsn: str, dump_file: Path) -> None:
    """Restore a pg_dump custom-format backup."""
    parsed = urlparse(dsn)
    pg_env = dict(os.environ)
    pg_env["PGPASSWORD"] = parsed.password or ""
    host = parsed.hostname or "127.0.0.1"
    port = str(parsed.port or 5432)
    user = parsed.username or "traitor"
    dbname = parsed.path.lstrip("/")

    cmd = [
        "pg_restore",
        "-h", host, "-p", port, "-U", user,
        "-d", dbname,
        "--clean", "--if-exists",  # drop before create (safe idempotent restore)
        "--no-owner",  # don't try to set object ownership
        "--single-transaction",
        str(dump_file),
    ]
    result = subprocess.run(cmd, env=pg_env, capture_output=True, text=True)
    if result.returncode != 0:
        # pg_restore returns non-zero even on warnings — check for real errors
        if "error" in result.stderr.lower() and "warning" not in result.stderr.lower():
            raise RuntimeError(f"pg_restore failed: {result.stderr.strip()}")
        print(f"    pg_restore completed with warnings")


def _import_redis(env: dict[str, str], redis_file: Path) -> None:
    """Import Redis state from a JSON export."""
    import redis as _redis

    host, port, password = _get_redis_url(env)
    r = _redis.Redis(host=host, port=port, password=password, decode_responses=True)
    r.ping()

    with open(redis_file) as f:
        data = json.load(f)

    pipe = r.pipeline()
    count = 0
    for key, info in data.items():
        ktype = info.get("type", "string")
        ttl = info.get("ttl", -1)

        if ktype == "string":
            if ttl > 0:
                pipe.setex(key, ttl, info["value"])
            else:
                pipe.set(key, info["value"])
        elif ktype == "list":
            pipe.delete(key)
            if info["value"]:
                pipe.rpush(key, *info["value"])
                if ttl > 0:
                    pipe.expire(key, ttl)
        elif ktype == "hash":
            pipe.delete(key)
            if info["value"]:
                pipe.hset(key, mapping=info["value"])
                if ttl > 0:
                    pipe.expire(key, ttl)
        elif ktype == "set":
            pipe.delete(key)
            if info["value"]:
                pipe.sadd(key, *info["value"])
                if ttl > 0:
                    pipe.expire(key, ttl)
        count += 1

    pipe.execute()
    print(f"    {count} keys restored")


# ---------------------------------------------------------------------------
# Redis key migration (old unprefixed → new profile-prefixed)
# ---------------------------------------------------------------------------

# Keys that were changed from unprefixed to {profile}:{key} in
# the domain-separation update (2026-03-08).
_REDIS_KEYS_TO_MIGRATE = [
    "trailing_stops:state",
    "dashboard:commands_queue",
    "dashboard:command_history",
    "agent:state",
    "agent:rules_status",
    "agent:pending_approvals",
    "news:watched_tickers",
    "news:latest",
]


def cmd_redis_migrate(args: argparse.Namespace) -> None:
    """Migrate Redis keys from old unprefixed format to profile-prefixed format.

    Run this ONCE after rebuilding containers with the domain-separation update.
    Safe to run multiple times (idempotent).
    """
    import redis as _redis

    env = _load_env()
    profile = args.profile
    dry = args.dry_run

    host, port, password = _get_redis_url(env)
    r = _redis.Redis(host=host, port=port, password=password, decode_responses=True)
    r.ping()

    migrated = 0
    skipped = 0

    for old_key in _REDIS_KEYS_TO_MIGRATE:
        new_key = f"{profile}:{old_key}"

        old_exists = r.exists(old_key)
        new_exists = r.exists(new_key)

        if not old_exists:
            continue

        if new_exists:
            print(f"  SKIP  {old_key} → {new_key} (new key already exists)")
            skipped += 1
            continue

        ttl = r.ttl(old_key)
        ktype = r.type(old_key)

        if not dry:
            # RENAME is atomic
            r.rename(old_key, new_key)
            # Preserve original TTL if it had one
            if ttl > 0:
                r.expire(new_key, ttl)

        print(f"  {'WOULD MIGRATE' if dry else 'MIGRATED'}  {old_key} → {new_key} (type={ktype}, ttl={ttl})")
        migrated += 1

    # Also check news:latest → news:{profile}:latest (different pattern)
    old_news = "news:latest"
    new_news = f"news:{profile}:latest"
    if r.exists(old_news) and not r.exists(new_news):
        ttl = r.ttl(old_news)
        if not dry:
            r.rename(old_news, new_news)
            if ttl > 0:
                r.expire(new_news, ttl)
        print(f"  {'WOULD MIGRATE' if dry else 'MIGRATED'}  {old_news} → {new_news} (ttl={ttl})")
        migrated += 1

    print(f"\n{'DRY RUN: ' if dry else ''}Migrated: {migrated}, Skipped: {skipped}")
    if dry and migrated > 0:
        print("Run without --dry-run to apply changes.")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Auto-Traitor data migration tool",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
    # Export all data
    python scripts/migrate_data.py export --out ./backups/2026-03-08

    # Import on the target machine
    python scripts/migrate_data.py import --from ./backups/2026-03-08

    # Migrate Redis keys after container rebuild
    python scripts/migrate_data.py redis-migrate --profile coinbase

    # Dry-run any command
    python scripts/migrate_data.py export --out /tmp/test --dry-run
        """,
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # export
    p_export = sub.add_parser("export", help="Export all data to backup directory")
    p_export.add_argument("--out", required=True, help="Output directory path")
    p_export.add_argument("--dry-run", action="store_true", help="Preview without writing")

    # import
    p_import = sub.add_parser("import", help="Import data from backup directory")
    p_import.add_argument("--from", required=True, dest="from", help="Backup directory path")
    p_import.add_argument("--dry-run", action="store_true", help="Preview without writing")

    # redis-migrate
    p_redis = sub.add_parser("redis-migrate", help="Migrate Redis keys to profile-prefixed format")
    p_redis.add_argument("--profile", required=True, help="Profile name (e.g., coinbase, ibkr)")
    p_redis.add_argument("--dry-run", action="store_true", help="Preview without writing")

    args = parser.parse_args()

    if args.command == "export":
        cmd_export(args)
    elif args.command == "import":
        cmd_import(args)
    elif args.command == "redis-migrate":
        cmd_redis_migrate(args)


if __name__ == "__main__":
    main()
