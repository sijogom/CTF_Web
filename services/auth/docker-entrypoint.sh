#!/bin/sh
set -eu

# Compose file-based secrets retain the host file owner and mode. The private
# PEM enters as a root-readable bind mount, then exists only in this tmpfs
# directory as an appuser-readable runtime copy. It is never copied into the
# image, release tree, or an environment variable.
readonly SECRET_SOURCE="/run/secrets/auth_jwt_private_key"
readonly RUNTIME_SECRET_DIR="/run/ctf-runtime-secrets"
readonly RUNTIME_SECRET_FILE="${RUNTIME_SECRET_DIR}/jwt_private.pem"

if [ ! -r "$SECRET_SOURCE" ]; then
    echo "JWT private-key runtime secret is unavailable." >&2
    exit 78
fi

# Keep the directory root-owned while the privileged entrypoint copies the
# file. With all capabilities dropped, changing it to appuser:appuser first
# would prevent root from stat-ing the destination. Lock the directory down
# only after the copy succeeds, before dropping privileges.
install -d -m 0700 "$RUNTIME_SECRET_DIR"
cp "$SECRET_SOURCE" "$RUNTIME_SECRET_FILE"
chmod 0600 "$RUNTIME_SECRET_FILE"
chown appuser:appuser "$RUNTIME_SECRET_FILE"
chown appuser:appuser "$RUNTIME_SECRET_DIR"

exec gosu appuser "$@"
