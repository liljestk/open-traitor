#!/bin/bash
# Auto-Traitor Dashboard entrypoint.
# On first startup (no DASHBOARD_PASSWORD_HASH in config/.env), generates a
# secure setup password, writes its bcrypt hash to config/.env, and prints
# the plaintext password prominently so the operator can log in to /setup.
set -e

CONFIG_DIR="/app/config"
CONFIG_ENV="$CONFIG_DIR/.env"

mkdir -p "$CONFIG_DIR"

# Generate setup password if no hash is present yet
if ! grep -q "^DASHBOARD_PASSWORD_HASH=" "$CONFIG_ENV" 2>/dev/null; then
    # Generate a 20-char alphanumeric password
    SETUP_PASS=$(python3 - <<'PYEOF'
import secrets, string
chars = string.ascii_letters + string.digits
print(''.join(secrets.choice(chars) for _ in range(20)))
PYEOF
)

    # Hash with bcrypt
    PASS_HASH=$(python3 - "$SETUP_PASS" <<'PYEOF'
import bcrypt, sys
pw = sys.argv[1]
h = bcrypt.hashpw(pw.encode(), bcrypt.gensalt()).decode()
print(h)
PYEOF
)

    # Escape $ as $$ so docker-compose env_file parsing doesn't
    # interpret bcrypt cost markers as variable references.
    PASS_HASH_ESCAPED="${PASS_HASH//\$/\$\$}"

    # Append to config/.env
    {
        printf '\n'
        printf '# Dashboard password (auto-generated on first startup — %s)\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
        printf 'DASHBOARD_PASSWORD_HASH=%s\n' "$PASS_HASH_ESCAPED"
    } >> "$CONFIG_ENV"

    # Print prominently to Docker logs
    echo ""
    echo "╔══════════════════════════════════════════════════════════════════╗"
    echo "║       🔑  AUTO-TRAITOR FIRST RUN — SETUP PASSWORD              ║"
    echo "╠══════════════════════════════════════════════════════════════════╣"
    echo "║                                                                  ║"
    echo "║  Open your browser and navigate to:                             ║"
    echo "║    http://localhost:8090/setup                                   ║"
    echo "║                                                                  ║"
    printf  "║  Setup Password:  %-44s  ║\n" "$SETUP_PASS"
    echo "║                                                                  ║"
    echo "║  ⚠  This password will NOT be shown again.                      ║"
    echo "║     You can change it later in Dashboard → Settings.            ║"
    echo "║                                                                  ║"
    echo "╚══════════════════════════════════════════════════════════════════╝"
    echo ""
fi

exec uvicorn src.dashboard.server:app \
    --host 0.0.0.0 \
    --port 8090 \
    --log-level warning
