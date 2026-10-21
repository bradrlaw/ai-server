#!/usr/bin/env bash
# Resets or displays the Hermes dashboard (:9119) basic-auth password.
#
# The PLAINTEXT password lives only in the gnome-keyring (service=hermes
# key=dashboard-password) -- it is never written to docker/.env or any file on
# disk. docker/.env only ever holds the scrypt HASH
# (HERMES_DASHBOARD_PASSWORD_HASH), which Hermes's basic_auth plugin verifies
# against (HERMES_DASHBOARD_BASIC_AUTH_PASSWORD_HASH), same pattern as a
# bcrypt hash in a password db -- not reversible, safe to keep in the compose
# env file.
#
# Usage:
#   scripts/hermes-dashboard-password.sh set    # generate + store a new password, recreate hermes
#   scripts/hermes-dashboard-password.sh show    # print current password (from keyring, for login)
set -euo pipefail

SERVICE=hermes
KEY=dashboard-password
ENV_FILE=/srv/ai/docker/.env
COMPOSE_DIR=/srv/ai/docker

cmd="${1:-}"

case "$cmd" in
  set)
    new_pass="$(openssl rand -base64 24 | tr -d '/+=')"
    hash="$(docker exec hermes python3 -c "
import sys
sys.path.insert(0, '/opt/hermes')
from plugins.dashboard_auth.basic import hash_password
print(hash_password(sys.argv[1]))
" "$new_pass")"

    printf '%s' "$new_pass" | secret-tool store --label='Hermes dashboard password' service "$SERVICE" key "$KEY"

    python3 - "$hash" "$ENV_FILE" <<'EOF'
import sys
hash_val, path = sys.argv[1], sys.argv[2]
with open(path) as f:
    lines = f.readlines()
replaced = False
out = []
for line in lines:
    if line.startswith("HERMES_DASHBOARD_PASSWORD_HASH="):
        out.append(f"HERMES_DASHBOARD_PASSWORD_HASH={hash_val}\n")
        replaced = True
    else:
        out.append(line)
if not replaced:
    out.append(f"HERMES_DASHBOARD_PASSWORD_HASH={hash_val}\n")
with open(path, "w") as f:
    f.writelines(out)
EOF

    ( cd "$COMPOSE_DIR" && docker compose up -d hermes )
    echo "New password stored in the keyring and hermes recreated." >&2
    echo "Retrieve it any time with: $0 show" >&2
    ;;
  show)
    secret-tool lookup service "$SERVICE" key "$KEY" || {
      echo "No password in the keyring yet; run '$0 set' first." >&2
      exit 1
    }
    echo
    ;;
  *)
    echo "usage: $0 {set|show}" >&2
    exit 1
    ;;
esac
