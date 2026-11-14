#!/usr/bin/env bash
# shellcheck shell=bash
#
# ctfctl shared safety primitives.
# This file deliberately does not source an environment file: values such as
# passwords and private keys must only be handed to Docker Compose through its
# --env-file option and must never be echoed by this tool.

set -Eeuo pipefail
IFS=$'\n\t'
umask 077

readonly CTFCTL_PROJECT_NAME="ctf_web"
readonly CTFCTL_DEFAULT_RELEASE_ROOT="/opt/ctf-web/releases"
readonly CTFCTL_DEFAULT_CURRENT_LINK="/opt/ctf-web/current"
readonly CTFCTL_DEFAULT_STATE_DIR="/var/lib/ctf-web/ctfctl"
readonly CTFCTL_DYNAMIC_FLAG_RUNTIME_ROOT="/var/lib/ctf-web"
readonly CTFCTL_DEFAULT_DYNAMIC_FLAG_RUNTIME_DIR="${CTFCTL_DYNAMIC_FLAG_RUNTIME_ROOT}/dynamic-flags"
readonly CTFCTL_ENV_ORCHESTRATOR_RUNTIME_UID="1001"
readonly CTFCTL_ENV_ORCHESTRATOR_RUNTIME_GID="1001"
readonly CTFCTL_CHALLENGE_NETWORK_NAME="ctf-challenges"
readonly CTFCTL_CHALLENGE_NETWORK_DRIVER="bridge"
readonly CTFCTL_CHALLENGE_NETWORK_SUBNET="172.30.0.0/16"
readonly CTFCTL_DEFAULT_ENV_FILE="/etc/ctf-web/ctf-web.env"
readonly CTFCTL_DEFAULT_CONTRACT_FILE="/etc/ctf-web/backup-contract.env"
readonly CTFCTL_SECRETS_DIR="/etc/ctf-web/secrets"
readonly CTFCTL_JWT_PRIVATE_KEY_NAME="jwt_private.pem"
readonly CTFCTL_JWT_PUBLIC_KEY_NAME="jwt_public.pem"
readonly CTFCTL_MANIFEST_SCHEMA="1"
readonly CTFCTL_BACKUP_CONTRACT_VERSION="2"
readonly CTFCTL_RELEASE_MANIFEST_SCHEMA="1"
readonly CTFCTL_RELEASE_MANIFEST_RELATIVE_PATH="infra/release-manifest.json"
readonly CTFCTL_RELEASE_FILES_RELATIVE_PATH="infra/release-files.sha256"
readonly CTFCTL_ARCHIVE_IMAGE="postgres:16-alpine"
readonly -a CTFCTL_ARCHIVED_VOLUME_LOGICAL_NAMES=(
    "redis_data"
    "minio_data"
    "caddy_data"
    "caddy_config"
    "ctfd_uploads"
)
readonly -a CTFCTL_DECLARED_PERSISTENT_VOLUME_LOGICAL_NAMES=(
    "postgres_data"
    "redis_data"
    "minio_data"
    "ctfd_uploads"
    "caddy_data"
    "caddy_config"
)
readonly -a CTFCTL_REQUIRED_RELEASE_RUNTIME_FILES=(
    "infra/docker-compose.yml"
    "infra/docker-compose.prod.yml"
    "infra/docker-compose.dynamic-flags.yml"
    "infra/caddy/Caddyfile"
    "infra/db/init/00-create-databases.sh"
)

CTFCTL_RELEASE_ROOT="${CTFCTL_RELEASE_ROOT:-$CTFCTL_DEFAULT_RELEASE_ROOT}"
CTFCTL_CURRENT_LINK="${CTFCTL_CURRENT_LINK:-$CTFCTL_DEFAULT_CURRENT_LINK}"
CTFCTL_STATE_DIR="${CTFCTL_STATE_DIR:-$CTFCTL_DEFAULT_STATE_DIR}"
CTFCTL_ENV_FILE="${CTFCTL_ENV_FILE:-$CTFCTL_DEFAULT_ENV_FILE}"
CTFCTL_CONTRACT_FILE="${CTFCTL_CONTRACT_FILE:-$CTFCTL_DEFAULT_CONTRACT_FILE}"
CTFCTL_CONTRACT_CTFD_DATABASE=""
CTFCTL_CONTRACT_CTFD_UPLOAD_VOLUME=""
CTFCTL_CONTRACT_CTFD_UPLOAD_MOUNT=""

ctfctl_log() {
    local level="$1"
    local code="$2"
    local message="$3"
    printf 'CTFCTL_%s code=%s message=%s\n' "$level" "$code" "$message"
}

ctfctl_info() {
    ctfctl_log "INFO" "$1" "$2"
}

ctfctl_ok() {
    ctfctl_log "OK" "$1" "$2"
}

ctfctl_warn() {
    ctfctl_log "WARN" "$1" "$2" >&2
}

ctfctl_die() {
    ctfctl_log "ERROR" "$1" "$2" >&2
    exit "${3:-1}"
}

ctfctl_require_command() {
    local command_name="$1"
    command -v "$command_name" >/dev/null 2>&1 || ctfctl_die "COMMAND_MISSING" "required command is unavailable: ${command_name}"
}

ctfctl_require_linux() {
    [[ "$(uname -s)" == "Linux" ]] || ctfctl_die "LINUX_REQUIRED" "ctfctl is only supported on a Linux host"
}

ctfctl_is_safe_path_text() {
    local path="$1"
    [[ "$path" =~ ^/[A-Za-z0-9._/-]+$ ]] || return 1
    [[ "$path" != "/" ]] || return 1
    [[ "$path" != *"//"* ]] || return 1
    [[ "/${path}/" != *"/../"* ]] || return 1
    [[ "/${path}/" != *"/./"* ]] || return 1
}

ctfctl_is_safe_relative_path() {
    local path="$1"
    [[ "$path" =~ ^[A-Za-z0-9][A-Za-z0-9._/-]*$ ]] || return 1
    [[ "$path" != *"//"* ]] || return 1
    [[ "/${path}/" != *"/../"* ]] || return 1
    [[ "/${path}/" != *"/./"* ]] || return 1
}

ctfctl_require_safe_absolute_path() {
    local path="$1"
    ctfctl_is_safe_path_text "$path" || ctfctl_die "UNSAFE_PATH" "only normalized absolute paths using letters, digits, dot, underscore, slash, and dash are accepted"
}

ctfctl_resolve_existing_dir() {
    local path="$1"
    ctfctl_require_safe_absolute_path "$path"
    [[ -d "$path" ]] || ctfctl_die "DIRECTORY_MISSING" "required directory does not exist"
    [[ ! -L "$path" ]] || ctfctl_die "SYMLINK_DIRECTORY_REJECTED" "directory arguments must not be symlinks"
    realpath -e -- "$path"
}

ctfctl_require_private_dir() {
    local path="$1"
    local resolved_path
    local mode
    resolved_path="$(ctfctl_resolve_existing_dir "$path")"
    mode="$(stat -c '%a' -- "$resolved_path")"
    [[ "${mode: -1}" != "2" && "${mode: -1}" != "3" && "${mode: -1}" != "6" && "${mode: -1}" != "7" ]] || ctfctl_die "DIRECTORY_WORLD_WRITABLE" "directory must not be writable by other users"
    [[ "${mode: -2:1}" != "2" && "${mode: -2:1}" != "3" && "${mode: -2:1}" != "6" && "${mode: -2:1}" != "7" ]] || ctfctl_die "DIRECTORY_GROUP_WRITABLE" "directory must not be writable by its group"
    printf '%s\n' "$resolved_path"
}

ctfctl_create_private_dir_if_missing() {
    local path="$1"
    local parent
    ctfctl_require_safe_absolute_path "$path"
    if [[ -e "$path" || -L "$path" ]]; then
        ctfctl_require_private_dir "$path" >/dev/null
        return 0
    fi
    parent="$(dirname -- "$path")"
    ctfctl_require_private_dir "$parent" >/dev/null
    mkdir -m 0750 -- "$path"
    ctfctl_require_private_dir "$path" >/dev/null
}

ctfctl_resolve_existing_file() {
    local path="$1"
    ctfctl_require_safe_absolute_path "$path"
    [[ -f "$path" ]] || ctfctl_die "FILE_MISSING" "required file does not exist"
    [[ ! -L "$path" ]] || ctfctl_die "SYMLINK_FILE_REJECTED" "file arguments must not be symlinks"
    realpath -e -- "$path"
}

ctfctl_require_immutable_release_file() {
    local path="$1"
    local resolved_path
    local owner
    local mode

    resolved_path="$(ctfctl_resolve_existing_file "$path")"
    owner="$(stat -c '%u' -- "$resolved_path")"
    mode="$(stat -c '%a' -- "$resolved_path")"
    [[ "$owner" == "0" ]] || ctfctl_die "RELEASE_FILE_OWNER_INVALID" "release files must be owned by root before deployment"
    [[ "${mode: -1}" != "2" && "${mode: -1}" != "3" && "${mode: -1}" != "6" && "${mode: -1}" != "7" ]] \
        || ctfctl_die "RELEASE_FILE_WORLD_WRITABLE" "release files must not be writable by other users"
    [[ "${mode: -2:1}" != "2" && "${mode: -2:1}" != "3" && "${mode: -2:1}" != "6" && "${mode: -2:1}" != "7" ]] \
        || ctfctl_die "RELEASE_FILE_GROUP_WRITABLE" "release files must not be writable by their group"
    printf '%s\n' "$resolved_path"
}

ctfctl_is_descendant() {
    local child="$1"
    local parent="$2"
    [[ "$child" == "$parent"/* ]]
}

ctfctl_resolve_new_path() {
    local path="$1"
    local parent
    local resolved_parent
    ctfctl_require_safe_absolute_path "$path"
    [[ ! -e "$path" && ! -L "$path" ]] || ctfctl_die "TARGET_EXISTS" "refusing to overwrite an existing target"
    parent="$(dirname -- "$path")"
    resolved_parent="$(ctfctl_require_private_dir "$parent")"
    printf '%s/%s\n' "$resolved_parent" "$(basename -- "$path")"
}

ctfctl_require_safe_id() {
    local identifier="$1"
    local label="$2"
    [[ "$identifier" =~ ^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$ ]] || ctfctl_die "UNSAFE_IDENTIFIER" "${label} must be a simple identifier"
}

ctfctl_release_id() {
    basename -- "$1"
}

ctfctl_validate_release() {
    local requested_release="$1"
    local release_root
    local release
    local permissions
    local owner

    release_root="$(ctfctl_require_private_dir "$CTFCTL_RELEASE_ROOT")"
    release="$(ctfctl_require_private_dir "$requested_release")"
    ctfctl_is_descendant "$release" "$release_root" || ctfctl_die "RELEASE_OUTSIDE_ROOT" "release must be a child of the configured release root"
    ctfctl_require_safe_id "$(ctfctl_release_id "$release")" "release id"
    [[ -f "$release/infra/docker-compose.yml" ]] || ctfctl_die "RELEASE_COMPOSE_MISSING" "release does not contain infra/docker-compose.yml"
    [[ -f "$release/infra/docker-compose.prod.yml" ]] || ctfctl_die "RELEASE_PROD_COMPOSE_MISSING" "release does not contain infra/docker-compose.prod.yml"
    [[ -f "$release/infra/docker-compose.dynamic-flags.yml" ]] || ctfctl_die "RELEASE_DYNAMIC_FLAGS_COMPOSE_MISSING" "release does not contain docker-compose.dynamic-flags.yml"

    owner="$(stat -c '%u' -- "$release")"
    permissions="$(stat -c '%a' -- "$release")"
    [[ "$owner" == "0" ]] || ctfctl_die "RELEASE_OWNER_INVALID" "release directories must be owned by root before deployment"
    [[ "${permissions: -1}" != "2" && "${permissions: -1}" != "3" && "${permissions: -1}" != "6" && "${permissions: -1}" != "7" ]] || ctfctl_die "RELEASE_WORLD_WRITABLE" "release must not be writable by other users"
    [[ "${permissions: -2:1}" != "2" && "${permissions: -2:1}" != "3" && "${permissions: -2:1}" != "6" && "${permissions: -2:1}" != "7" ]] || ctfctl_die "RELEASE_GROUP_WRITABLE" "release must not be writable by its group"
    [[ ! -d "$release/.git" ]] || ctfctl_die "RELEASE_NOT_IMMUTABLE" "a release must not contain a repository metadata directory"
    ctfctl_require_immutable_release_file "$release/infra/docker-compose.yml" >/dev/null
    ctfctl_require_immutable_release_file "$release/infra/docker-compose.prod.yml" >/dev/null
    ctfctl_require_immutable_release_file "$release/infra/docker-compose.dynamic-flags.yml" >/dev/null
    printf '%s\n' "$release"
}

ctfctl_json_string_value() {
    local json_file="$1"
    local key="$2"
    local line
    local value=""
    local pattern

    # Release and bundle manifests use a deliberately small, flat JSON subset.
    # Do not invoke a parser that can execute hooks or expand a value. A value
    # may contain only the characters that are valid in our release identifiers,
    # SHA-256 strings, and image references.
    [[ "$key" =~ ^[A-Za-z0-9_]+$ ]] || return 1
    pattern="^[[:space:]]*\\\"${key}\\\"[[:space:]]*:[[:space:]]*\\\"([A-Za-z0-9._:@/-]+)\\\"[[:space:]]*,?[[:space:]]*$"
    while IFS= read -r line; do
        if [[ "$line" =~ $pattern ]]; then
            [[ -z "$value" ]] || return 1
            value="${BASH_REMATCH[1]}"
        fi
    done < "$json_file"
    [[ -n "$value" ]] || return 1
    printf '%s\n' "$value"
}

ctfctl_release_manifest_path() {
    local release="$1"
    printf '%s/%s\n' "$release" "$CTFCTL_RELEASE_MANIFEST_RELATIVE_PATH"
}

ctfctl_assert_release_files_contract() {
    local release="$1"
    local expected_checksum_file_hash="$2"
    local checksum_file
    local actual_checksum_file_hash
    local line
    local relative_path
    local resolved_file
    local required_path
    local file_count=0
    local -A seen_paths=()

    [[ "$expected_checksum_file_hash" =~ ^[0-9a-f]{64}$ ]] || ctfctl_die "RELEASE_FILES_MANIFEST_HASH_INVALID" "release manifest runtime-files hash is invalid"
    checksum_file="$(ctfctl_require_immutable_release_file "$release/$CTFCTL_RELEASE_FILES_RELATIVE_PATH")"
    actual_checksum_file_hash="$(ctfctl_sha256_file "$checksum_file")"
    [[ "$actual_checksum_file_hash" == "$expected_checksum_file_hash" ]] \
        || ctfctl_die "RELEASE_FILES_MANIFEST_MISMATCH" "release manifest does not match the runtime file checksum list"

    while IFS= read -r line; do
        [[ "$line" =~ ^[0-9a-f]{64}[[:space:]][[:space:]]([A-Za-z0-9._/-]+)$ ]] \
            || ctfctl_die "RELEASE_FILES_MANIFEST_INVALID" "release runtime file checksum list contains an unsafe entry"
        relative_path="${BASH_REMATCH[1]}"
        ctfctl_is_safe_relative_path "$relative_path" \
            || ctfctl_die "RELEASE_FILES_MANIFEST_INVALID" "release runtime file checksum list contains an unsafe path"
        [[ -z "${seen_paths[$relative_path]:-}" ]] \
            || ctfctl_die "RELEASE_FILES_MANIFEST_INVALID" "release runtime file checksum list contains a duplicate path"
        seen_paths["$relative_path"]=1
        resolved_file="$(ctfctl_require_immutable_release_file "$release/$relative_path")"
        ctfctl_is_descendant "$resolved_file" "$release" \
            || ctfctl_die "RELEASE_FILES_MANIFEST_ESCAPE" "release runtime file checksum list resolves outside the selected release"
        file_count=$((file_count + 1))
    done < "$checksum_file"
    (( file_count > 0 )) || ctfctl_die "RELEASE_FILES_MANIFEST_EMPTY" "release runtime file checksum list is empty"

    for required_path in "${CTFCTL_REQUIRED_RELEASE_RUNTIME_FILES[@]}"; do
        [[ -n "${seen_paths[$required_path]:-}" ]] \
            || ctfctl_die "RELEASE_RUNTIME_FILE_UNMANIFESTED" "release runtime file checksum list omits a required non-secret runtime input"
    done

    (
        cd -- "$release"
        sha256sum --check --status "$CTFCTL_RELEASE_FILES_RELATIVE_PATH"
    ) || ctfctl_die "RELEASE_FILES_CHECKSUM_FAILED" "release runtime file checksum verification failed"
}

ctfctl_assert_release_manifest_contract() {
    local release="$1"
    local manifest
    local release_id
    local manifest_release_id
    local manifest_schema
    local manifest_project
    local manifest_compose_hash
    local manifest_compose_prod_hash
    local manifest_runtime_files_hash
    local compose_hash
    local compose_prod_hash

    manifest="$(ctfctl_release_manifest_path "$release")"
    manifest="$(ctfctl_require_immutable_release_file "$manifest")"
    ctfctl_is_descendant "$manifest" "$release" || ctfctl_die "RELEASE_MANIFEST_OUTSIDE_RELEASE" "release manifest must stay inside the selected release"
    release_id="$(ctfctl_release_id "$release")"
    manifest_schema="$(ctfctl_json_string_value "$manifest" "schema_version" || true)"
    manifest_release_id="$(ctfctl_json_string_value "$manifest" "release_id" || true)"
    manifest_project="$(ctfctl_json_string_value "$manifest" "project" || true)"
    manifest_compose_hash="$(ctfctl_json_string_value "$manifest" "compose_sha256" || true)"
    manifest_compose_prod_hash="$(ctfctl_json_string_value "$manifest" "compose_prod_sha256" || true)"
    manifest_runtime_files_hash="$(ctfctl_json_string_value "$manifest" "runtime_files_sha256" || true)"

    [[ "$manifest_schema" == "$CTFCTL_RELEASE_MANIFEST_SCHEMA" ]] || ctfctl_die "RELEASE_MANIFEST_SCHEMA_UNSUPPORTED" "release manifest schema is missing or unsupported"
    [[ "$manifest_release_id" == "$release_id" ]] || ctfctl_die "RELEASE_MANIFEST_RELEASE_MISMATCH" "release manifest does not bind the selected release id"
    [[ "$manifest_project" == "$CTFCTL_PROJECT_NAME" ]] || ctfctl_die "RELEASE_MANIFEST_PROJECT_MISMATCH" "release manifest does not bind the fixed ctf_web project"
    [[ "$manifest_compose_hash" =~ ^[0-9a-f]{64}$ ]] || ctfctl_die "RELEASE_MANIFEST_HASH_INVALID" "release manifest compose hash is invalid"
    [[ "$manifest_compose_prod_hash" =~ ^[0-9a-f]{64}$ ]] || ctfctl_die "RELEASE_MANIFEST_HASH_INVALID" "release manifest production compose hash is invalid"

    compose_hash="$(ctfctl_sha256_file "$release/infra/docker-compose.yml")"
    compose_prod_hash="$(ctfctl_sha256_file "$release/infra/docker-compose.prod.yml")"
    [[ "$manifest_compose_hash" == "$compose_hash" ]] || ctfctl_die "RELEASE_MANIFEST_COMPOSE_MISMATCH" "release manifest does not match docker-compose.yml"
    [[ "$manifest_compose_prod_hash" == "$compose_prod_hash" ]] || ctfctl_die "RELEASE_MANIFEST_COMPOSE_PROD_MISMATCH" "release manifest does not match docker-compose.prod.yml"
    ctfctl_assert_release_files_contract "$release" "$manifest_runtime_files_hash"
}

ctfctl_assert_declared_persistent_volume_contract() {
    local release="$1"
    local logical_name
    local discovered_name
    local -A expected_names=()
    local -A discovered_names=()

    for logical_name in "${CTFCTL_DECLARED_PERSISTENT_VOLUME_LOGICAL_NAMES[@]}"; do
        expected_names["$logical_name"]=1
    done
    while IFS= read -r discovered_name; do
        [[ -n "$discovered_name" ]] || continue
        [[ -n "${expected_names[$discovered_name]:-}" ]] \
            || ctfctl_die "PERSISTENT_VOLUME_UNDECLARED" "release declares a persistent volume outside the reviewed backup contract"
        [[ -z "${discovered_names[$discovered_name]:-}" ]] \
            || ctfctl_die "PERSISTENT_VOLUME_DECLARATION_DUPLICATE" "release declares a persistent volume more than once"
        discovered_names["$discovered_name"]=1
    done < <(
        awk '
            /^volumes:[[:space:]]*$/ { inside = 1; next }
            inside && /^[^[:space:]]/ { exit }
            inside && /^  [a-z0-9_]+:[[:space:]]*$/ {
                name = $0
                sub(/^  /, "", name)
                sub(/:.*/, "", name)
                print name
            }
        ' "$release/infra/docker-compose.yml"
    )
    for logical_name in "${CTFCTL_DECLARED_PERSISTENT_VOLUME_LOGICAL_NAMES[@]}"; do
        [[ -n "${discovered_names[$logical_name]:-}" ]] \
            || ctfctl_die "PERSISTENT_VOLUME_UNDECLARED" "release does not declare a required persistent volume"
    done
    grep -Fqx "      - ctfd_uploads:/var/uploads" "$release/infra/docker-compose.yml" \
        || ctfctl_die "CTFD_UPLOAD_MOUNT_UNDECLARED" "release does not declare the CTFd upload mount at /var/uploads"
}

ctfctl_active_release() {
    local current_parent
    local target
    ctfctl_require_safe_absolute_path "$CTFCTL_CURRENT_LINK"
    ctfctl_is_descendant "$CTFCTL_CURRENT_LINK" "/opt/ctf-web" || ctfctl_die "CURRENT_LINK_OUTSIDE_ROOT" "current release link must stay under /opt/ctf-web"
    current_parent="$(dirname -- "$CTFCTL_CURRENT_LINK")"
    ctfctl_require_private_dir "$current_parent" >/dev/null
    [[ -L "$CTFCTL_CURRENT_LINK" ]] || ctfctl_die "CURRENT_RELEASE_MISSING" "configured current release link is missing"
    target="$(realpath -e -- "$CTFCTL_CURRENT_LINK")"
    ctfctl_validate_release "$target"
}

ctfctl_require_state_dir_scope() {
    ctfctl_require_safe_absolute_path "$CTFCTL_STATE_DIR"
    ctfctl_is_descendant "$CTFCTL_STATE_DIR" "/var/lib/ctf-web" || ctfctl_die "STATE_DIR_OUTSIDE_SCOPE" "state directory must stay under /var/lib/ctf-web"
}

ctfctl_require_env_file() {
    local env_file
    env_file="$(ctfctl_resolve_existing_file "$CTFCTL_ENV_FILE")"
    ctfctl_require_private_dir "$(dirname -- "$env_file")" >/dev/null
    local mode
    mode="$(stat -c '%a' -- "$env_file")"
    [[ "${mode: -1}" != "4" && "${mode: -1}" != "5" && "${mode: -1}" != "6" && "${mode: -1}" != "7" ]] || ctfctl_die "ENV_FILE_WORLD_READABLE" "environment file must not be readable by other users"
    printf '%s\n' "$env_file"
}

ctfctl_env_has_nonempty_value() {
    local env_file="$1"
    local key="$2"
    # No value is emitted. Quotes are accepted but an empty assignment is not.
    grep -Eq "^[[:space:]]*${key}=.+$" "$env_file"
}

ctfctl_env_has_insecure_default() {
    local env_file="$1"
    local key="$2"

    # Production release gates must not accept the development placeholders
    # that remain available in the base Compose file. Only a boolean result is
    # returned; the matched value is never printed or copied into a log.
    case "$key" in
        INTERNAL_API_KEY|POSTGRES_PASSWORD|AUTH_DB_PASSWORD|LEARN_DB_PASSWORD|COMMUNITY_DB_PASSWORD|REDIS_PASSWORD)
            grep -Eq "^[[:space:]]*${key}=(changeme|changeme_auth|changeme_learn|changeme_community)[[:space:]]*$" "$env_file"
            ;;
        MINIO_ROOT_USER)
            grep -Eq "^[[:space:]]*${key}=minioadmin[[:space:]]*$" "$env_file"
            ;;
        MINIO_ROOT_PASSWORD)
            grep -Eq "^[[:space:]]*${key}=changeme[[:space:]]*$" "$env_file"
            ;;
        CTFD_SECRET_KEY)
            grep -Eq "^[[:space:]]*${key}=ctfd-dev-secret-change-me[[:space:]]*$" "$env_file"
            ;;
        SITE_DOMAIN)
            grep -Eq "^[[:space:]]*${key}=(localhost|127\\.0\\.0\\.1)[[:space:]]*$" "$env_file"
            ;;
        *)
            return 1
            ;;
    esac
}

ctfctl_env_safe_path_value() {
    local env_file="$1"
    local key="$2"
    local line
    local value

    # Do not source the environment file. Only a single unquoted, normalized
    # absolute path is accepted for a secret source reference.
    line="$(grep -E "^[[:space:]]*${key}=/[A-Za-z0-9._/-]+[[:space:]]*$" "$env_file" | tail -n 1 || true)"
    [[ -n "$line" ]] || return 1
    value="${line#*=}"
    ctfctl_is_safe_path_text "$value" || return 1
    printf '%s\n' "$value"
}

ctfctl_dynamic_flag_runtime_dir_from_env() {
    local env_file="$1"
    local runtime_dir

    # Do not source the environment file. An omitted setting uses the reviewed
    # default; an explicit setting must be one normalized absolute path under
    # the managed runtime root, so Compose cannot bind an arbitrary host path.
    if grep -Eq '^[[:space:]]*DYNAMIC_FLAG_RUNTIME_DIR=' "$env_file"; then
        runtime_dir="$(ctfctl_env_safe_path_value "$env_file" "DYNAMIC_FLAG_RUNTIME_DIR")" || {
            ctfctl_warn "DYNAMIC_FLAG_RUNTIME_DIR_INVALID" "DYNAMIC_FLAG_RUNTIME_DIR must be one normalized absolute path; its value was not printed"
            return 1
        }
    else
        runtime_dir="$CTFCTL_DEFAULT_DYNAMIC_FLAG_RUNTIME_DIR"
    fi

    [[ "$runtime_dir" == "$CTFCTL_DYNAMIC_FLAG_RUNTIME_ROOT" || "$runtime_dir" == "$CTFCTL_DYNAMIC_FLAG_RUNTIME_ROOT/"* ]] || {
        ctfctl_warn "DYNAMIC_FLAG_RUNTIME_DIR_OUTSIDE_SCOPE" "DYNAMIC_FLAG_RUNTIME_DIR must stay under the managed runtime root"
        return 1
    }
    printf '%s\n' "$runtime_dir"
}

ctfctl_require_dynamic_flag_runtime_dir() {
    local runtime_dir="$1"
    local resolved_dir
    local owner
    local group
    local mode

    ctfctl_require_safe_absolute_path "$runtime_dir"
    [[ "$runtime_dir" == "$CTFCTL_DYNAMIC_FLAG_RUNTIME_ROOT" || "$runtime_dir" == "$CTFCTL_DYNAMIC_FLAG_RUNTIME_ROOT/"* ]] \
        || ctfctl_die "DYNAMIC_FLAG_RUNTIME_DIR_OUTSIDE_SCOPE" "dynamic flag runtime directory must stay under the managed runtime root"
    resolved_dir="$(ctfctl_resolve_existing_dir "$runtime_dir")"
    [[ "$resolved_dir" == "$CTFCTL_DYNAMIC_FLAG_RUNTIME_ROOT" || "$resolved_dir" == "$CTFCTL_DYNAMIC_FLAG_RUNTIME_ROOT/"* ]] \
        || ctfctl_die "DYNAMIC_FLAG_RUNTIME_DIR_SYMLINK_ESCAPE" "dynamic flag runtime directory must resolve inside the managed runtime root"
    owner="$(stat -c '%u' -- "$resolved_dir")"
    group="$(stat -c '%g' -- "$resolved_dir")"
    mode="$(stat -c '%a' -- "$resolved_dir")"
    [[ "$owner" == "$CTFCTL_ENV_ORCHESTRATOR_RUNTIME_UID" ]] \
        || ctfctl_die "DYNAMIC_FLAG_RUNTIME_DIR_OWNER_INVALID" "dynamic flag runtime directory must be owned by the env-orchestrator runtime user"
    [[ "$group" == "$CTFCTL_ENV_ORCHESTRATOR_RUNTIME_GID" ]] \
        || ctfctl_die "DYNAMIC_FLAG_RUNTIME_DIR_GROUP_INVALID" "dynamic flag runtime directory must use the env-orchestrator runtime group"
    [[ "$mode" == "700" ]] \
        || ctfctl_die "DYNAMIC_FLAG_RUNTIME_DIR_MODE_INVALID" "dynamic flag runtime directory must have mode 0700"
}

ctfctl_assert_dynamic_flag_runtime_contract() {
    local env_file="$1"
    local runtime_dir

    runtime_dir="$(ctfctl_dynamic_flag_runtime_dir_from_env "$env_file")" \
        || ctfctl_die "DYNAMIC_FLAG_RUNTIME_DIR_INVALID" "dynamic flag runtime directory configuration is invalid; its value was not printed"
    ctfctl_require_dynamic_flag_runtime_dir "$runtime_dir"
    ctfctl_ok "DYNAMIC_FLAG_RUNTIME_READY" "dynamic flag runtime directory owner, mode, and managed scope are valid"
}

ctfctl_prepare_default_dynamic_flag_runtime_dir() {
    local runtime_dir="$CTFCTL_DEFAULT_DYNAMIC_FLAG_RUNTIME_DIR"

    ctfctl_require_command "install"
    if [[ -e "$runtime_dir" || -L "$runtime_dir" ]]; then
        ctfctl_require_dynamic_flag_runtime_dir "$runtime_dir"
    else
        install -d -m 0700 -o "$CTFCTL_ENV_ORCHESTRATOR_RUNTIME_UID" -g "$CTFCTL_ENV_ORCHESTRATOR_RUNTIME_GID" -- "$runtime_dir" \
            || ctfctl_die "DYNAMIC_FLAG_RUNTIME_DIR_CREATE_FAILED" "could not create the managed dynamic flag runtime directory"
        ctfctl_require_dynamic_flag_runtime_dir "$runtime_dir"
    fi
    ctfctl_ok "DYNAMIC_FLAG_RUNTIME_PREPARED" "managed dynamic flag runtime directory is ready for the env-orchestrator"
}

ctfctl_check_jwt_key_source() {
    local env_file="$1"
    local env_key="$2"
    local expected_name="$3"
    local expected_mode="$4"
    local source_path
    local resolved_path
    local secrets_dir
    local owner
    local mode

    source_path="$(ctfctl_env_safe_path_value "$env_file" "$env_key")" || {
        ctfctl_warn "JWT_KEY_SOURCE_INVALID" "${env_key} must reference one normalized absolute path; its value was not printed"
        return 1
    }
    ctfctl_is_descendant "$source_path" "$CTFCTL_SECRETS_DIR" || {
        ctfctl_warn "JWT_KEY_SOURCE_OUTSIDE_SCOPE" "${env_key} must remain under the managed secrets directory"
        return 1
    }
    [[ "$(basename -- "$source_path")" == "$expected_name" ]] || {
        ctfctl_warn "JWT_KEY_SOURCE_NAME_INVALID" "${env_key} must use the approved key filename"
        return 1
    }

    secrets_dir="$(ctfctl_require_private_dir "$CTFCTL_SECRETS_DIR")"
    resolved_path="$(ctfctl_resolve_existing_file "$source_path")"
    ctfctl_is_descendant "$resolved_path" "$secrets_dir" || ctfctl_die "JWT_KEY_SOURCE_SYMLINK_ESCAPE" "JWT key source must resolve inside the managed secrets directory"
    [[ "$(basename -- "$resolved_path")" == "$expected_name" ]] || ctfctl_die "JWT_KEY_SOURCE_NAME_INVALID" "JWT key source must use the approved key filename"

    owner="$(stat -c '%u' -- "$resolved_path")"
    mode="$(stat -c '%a' -- "$resolved_path")"
    [[ "$owner" == "0" ]] || ctfctl_die "JWT_KEY_OWNER_INVALID" "JWT key source must be owned by root"
    [[ "$mode" == "$expected_mode" ]] || ctfctl_die "JWT_KEY_MODE_INVALID" "JWT key source permissions do not meet the managed-key contract"
    ctfctl_ok "JWT_KEY_SOURCE_READY" "${env_key} source exists with the required owner and mode"
}

ctfctl_check_jwt_key_sources() {
    local env_file="$1"
    ctfctl_check_jwt_key_source "$env_file" "JWT_SECRET_KEY_SOURCE" "$CTFCTL_JWT_PRIVATE_KEY_NAME" "600" || return 1
    ctfctl_check_jwt_key_source "$env_file" "JWT_PUBLIC_KEY_SOURCE" "$CTFCTL_JWT_PUBLIC_KEY_NAME" "644" || return 1
}

ctfctl_dynamic_flag_feature_is_enabled() {
    local env_file="$1"
    local feature_line
    local feature_value

    # Omitted means disabled. Do not source the environment file or emit its
    # values. Return 0 only for a valid enabled configuration, 1 for disabled,
    # and 2 for an invalid setting.
    feature_line="$(grep -E '^[[:space:]]*DYNAMIC_INSTANCE_FLAGS_ENABLED=(true|false|1|0)$' "$env_file" | tail -n 1 || true)"
    if [[ -z "$feature_line" ]]; then
        if grep -Eq '^[[:space:]]*DYNAMIC_INSTANCE_FLAGS_ENABLED=' "$env_file"; then
            return 2
        fi
        return 1
    fi

    feature_value="${feature_line#*=}"
    case "$feature_value" in
        true|1) return 0 ;;
        false|0) return 1 ;;
        *) return 2 ;;
    esac
}

ctfctl_check_dynamic_flag_feature_config() {
    local env_file="$1"
    local feature_state

    if ctfctl_dynamic_flag_feature_is_enabled "$env_file"; then
        if ctfctl_env_has_nonempty_value "$env_file" "DYNAMIC_FLAG_HMAC_KEYRING"; then
            ctfctl_ok "DYNAMIC_FLAG_KEYRING_PRESENT" "dynamic instance flag keyring is present"
            return 0
        fi
        ctfctl_warn "DYNAMIC_FLAG_KEYRING_MISSING" "dynamic instance flags are enabled but the keyring is absent or empty"
        return 1
    fi
    feature_state=$?

    if [[ "$feature_state" == "1" ]]; then
        ctfctl_ok "DYNAMIC_FLAG_FEATURE_DISABLED" "dynamic instance flags are disabled or omitted"
        return 0
    fi

    ctfctl_warn "DYNAMIC_FLAG_FEATURE_VALUE_INVALID" "DYNAMIC_INSTANCE_FLAGS_ENABLED must be true, false, 1, or 0; its value was not printed"
    return 1
}

ctfctl_assert_dynamic_flag_start_contract() {
    local env_file="$1"
    local feature_state

    ctfctl_check_dynamic_flag_feature_config "$env_file" \
        || ctfctl_die "DYNAMIC_FLAG_FEATURE_CONFIG_INVALID" "dynamic instance flag configuration is invalid; secret values were not printed"

    if ctfctl_dynamic_flag_feature_is_enabled "$env_file"; then
        ctfctl_assert_dynamic_flag_runtime_contract "$env_file"
        return 0
    fi
    feature_state=$?
    [[ "$feature_state" == "1" ]] && return 0
    ctfctl_die "DYNAMIC_FLAG_FEATURE_CONFIG_INVALID" "dynamic instance flag configuration is invalid; secret values were not printed"
}

ctfctl_assert_education_private_storage_contract() {
    local release="$1"
    local caddyfile
    local -a blocked_matches=()
    local -a generic_matches=()
    local blocked_line
    local generic_line

    # Caddy는 /ctf/storage/*의 공개 경계다. 실행 중인 container를 추측하거나 MinIO
    # 자격 증명을 읽지 않고 immutable release 파일만 확인한다. 실제 bucket policy
    # probe는 별도 승인된 runtime 검증 단계에서만 수행한다.
    caddyfile="$(ctfctl_require_immutable_release_file "$release/infra/caddy/Caddyfile")"
    grep -Fq 'path_regexp education_private_storage ^/ctf/storage/education-private-assets(?:/|$)' "$caddyfile" \
        || ctfctl_die "EDUCATION_PRIVATE_STORAGE_BLOCK_MISSING" "release에 education-private-assets storage 차단 경로가 없습니다"

    mapfile -t blocked_matches < <(grep -nF 'handle @education_private_storage {' "$caddyfile" || true)
    mapfile -t generic_matches < <(grep -nF 'handle /ctf/storage/* {' "$caddyfile" || true)
    (( ${#blocked_matches[@]} == 1 )) \
        || ctfctl_die "EDUCATION_PRIVATE_STORAGE_BLOCK_AMBIGUOUS" "release에는 교육용 비공개 storage 차단 block이 하나만 있어야 합니다"
    (( ${#generic_matches[@]} == 1 )) \
        || ctfctl_die "STORAGE_PROXY_AMBIGUOUS" "release에는 일반 storage proxy block이 하나만 있어야 합니다"

    blocked_line="${blocked_matches[0]%%:*}"
    generic_line="${generic_matches[0]%%:*}"
    (( blocked_line < generic_line )) \
        || ctfctl_die "EDUCATION_PRIVATE_STORAGE_ORDER_INVALID" "교육용 비공개 storage block은 일반 storage proxy보다 앞에 있어야 합니다"
    awk -v start="$blocked_line" -v end="$generic_line" '
        NR > start && NR < end && $0 ~ /^[[:space:]]*respond 404[[:space:]]*$/ { found = 1 }
        END { exit(found ? 0 : 1) }
    ' "$caddyfile" \
        || ctfctl_die "EDUCATION_PRIVATE_STORAGE_RESPONSE_INVALID" "교육용 비공개 storage block은 direct 요청에 404를 반환해야 합니다"

    ctfctl_ok "EDUCATION_PRIVATE_STORAGE_SOURCE_CONTRACT" "release가 일반 storage routing 전에 education-private-assets direct proxy를 차단합니다"
}

ctfctl_compose_service_contains() {
    local compose_file="$1"
    local service_name="$2"
    local required_text="$3"

    awk -v service="  ${service_name}:" -v required_text="$required_text" '
        $0 == service { inside_service = 1; next }
        inside_service && /^  [[:alnum:]_-]+:/ { exit }
        inside_service && index($0, required_text) > 0 { found = 1 }
        END { exit(found ? 0 : 1) }
    ' "$compose_file"
}

ctfctl_assert_env_orchestrator_build_context_host_contract() {
    local release="$1"
    local compose_file
    local production_compose_file

    # Env orchestrator는 host 네트워크를 사용하지만 Learn이 발급한 MinIO
    # presigned URL의 minio 호스트명을 그대로 사용해야 한다. URL host를
    # rewrite하면 서명이 무효화될 수 있으므로 immutable Compose 소스에 선언한
    # container-local hosts mapping만 허용한다.
    compose_file="$(ctfctl_require_immutable_release_file "$release/infra/docker-compose.yml")"
    production_compose_file="$(ctfctl_require_immutable_release_file "$release/infra/docker-compose.prod.yml")"

    ctfctl_compose_service_contains "$compose_file" "minio" '127.0.0.1:9000:9000' \
        || ctfctl_die "MINIO_LOOPBACK_PUBLISH_MISSING" "release must publish MinIO API only on host loopback for Env build-context access"
    ctfctl_compose_service_contains "$production_compose_file" "env-orchestrator" 'network_mode: host' \
        || ctfctl_die "ENV_HOST_NETWORK_CONTRACT_MISSING" "release Env orchestrator must retain its reviewed host-network contract"
    ctfctl_compose_service_contains "$production_compose_file" "env-orchestrator" 'extra_hosts:' \
        || ctfctl_die "ENV_MINIO_HOST_MAPPING_MISSING" "release Env orchestrator must declare the signed MinIO hostname mapping"
    ctfctl_compose_service_contains "$production_compose_file" "env-orchestrator" 'minio=127.0.0.1' \
        || ctfctl_die "ENV_MINIO_HOST_MAPPING_MISMATCH" "release Env orchestrator must map minio to the host loopback MinIO publish"
    ctfctl_compose_service_contains "$production_compose_file" "env-orchestrator" 'MINIO_REWRITE_HOST' \
        && ctfctl_die "MINIO_PRESIGNED_URL_REWRITE_FORBIDDEN" "release must not rewrite signed MinIO build-context URLs"

    ctfctl_ok "ENV_BUILD_CONTEXT_HOST_SOURCE_CONTRACT" "immutable Compose preserves signed MinIO build-context access for the host-network Env orchestrator"
}

ctfctl_minio_anonymous_status_code() {
    local url="$1"
    local status_code

    # --fail을 쓰지 않는다. 이 anonymous probe의 성공 조건은 401/403이며, 응답
    # 본문은 버린다. Authorization header, 자격 증명, 객체 내용은 요청·출력하지 않는다.
    status_code="$(curl \
        --silent \
        --output /dev/null \
        --write-out '%{http_code}' \
        --connect-timeout 3 \
        --max-time 8 \
        --proto '=http' \
        --request GET \
        "$url" 2>/dev/null || true)"
    [[ "$status_code" =~ ^[0-9]{3}$ ]] \
        || ctfctl_die "EDUCATION_PRIVATE_STORAGE_PROBE_UNAVAILABLE" "MinIO anonymous privacy probe가 HTTP 상태를 반환하지 않았습니다"
    printf '%s\n' "$status_code"
}

ctfctl_assert_education_private_bucket_anonymous_contract() {
    local release="$1"
    local env_file="$2"
    local list_status
    local object_status
    local minio_base_url="http://127.0.0.1:9000"
    local bucket="education-private-assets"

    # MinIO loopback API에 보내는 읽기 전용 anonymous 요청이다. 자격 증명 없는
    # 호출자가 버킷 목록 또는 임의의 존재하지 않는 object를 얻을 수 없는지 확인한다.
    # 정책 변경 API를 호출하지 않고, 환경 값을 읽거나 응답 본문을 출력하지 않는다.
    ctfctl_require_command "curl"
    ctfctl_require_service_running "$release" "$env_file" "minio"

    list_status="$(ctfctl_minio_anonymous_status_code "${minio_base_url}/${bucket}?list-type=2&max-keys=1")"
    case "$list_status" in
        401|403) ;;
        *) ctfctl_die "EDUCATION_PRIVATE_STORAGE_LIST_EXPOSED" "anonymous MinIO bucket-list probe가 거절되지 않았습니다" ;;
    esac

    object_status="$(ctfctl_minio_anonymous_status_code "${minio_base_url}/${bucket}/ctfctl-anonymous-probe-object")"
    case "$object_status" in
        401|403) ;;
        *) ctfctl_die "EDUCATION_PRIVATE_STORAGE_OBJECT_EXPOSED" "anonymous MinIO object probe가 거절되지 않았습니다" ;;
    esac

    ctfctl_ok "EDUCATION_PRIVATE_STORAGE_ANONYMOUS_DENIED" "자격 증명을 읽거나 storage를 변경하지 않은 anonymous MinIO list·arbitrary-object probe가 모두 거절되었습니다"
}

ctfctl_check_secret_references() {
    local env_file="$1"
    local -a required_keys=(
        "INTERNAL_API_KEY"
        "JWT_SECRET_KEY_SOURCE"
        "JWT_PUBLIC_KEY_SOURCE"
        "POSTGRES_PASSWORD"
        "AUTH_DB_PASSWORD"
        "LEARN_DB_PASSWORD"
        "COMMUNITY_DB_PASSWORD"
        "REDIS_PASSWORD"
        "MINIO_ROOT_USER"
        "MINIO_ROOT_PASSWORD"
        "CTFD_SECRET_KEY"
        "SITE_DOMAIN"
        "SMTP_HOST"
        "SMTP_FROM"
    )
    local key
    local missing=0

    for key in "${required_keys[@]}"; do
        if ctfctl_env_has_nonempty_value "$env_file" "$key"; then
            if ctfctl_env_has_insecure_default "$env_file" "$key"; then
                ctfctl_warn "SECRET_REFERENCE_INSECURE_DEFAULT" "required secret/config reference ${key} uses a development placeholder"
                missing=1
            else
                ctfctl_ok "SECRET_REFERENCE_PRESENT" "required secret/config reference ${key} is present"
            fi
        else
            ctfctl_warn "SECRET_REFERENCE_MISSING" "required secret/config reference ${key} is absent or empty"
            missing=1
        fi
    done
    (( missing == 0 )) || return 1
    ctfctl_check_jwt_key_sources "$env_file"
    ctfctl_check_dynamic_flag_feature_config "$env_file"
}

ctfctl_compose_args() {
    local release="$1"
    local env_file="$2"
    local feature_state
    printf '%s\0' \
        "--project-name" "$CTFCTL_PROJECT_NAME" \
        "--env-file" "$env_file" \
        "-f" "$release/infra/docker-compose.yml" \
        "-f" "$release/infra/docker-compose.prod.yml"

    if ctfctl_dynamic_flag_feature_is_enabled "$env_file"; then
        printf '%s\0' "-f" "$release/infra/docker-compose.dynamic-flags.yml"
        return 0
    fi
    feature_state=$?
    [[ "$feature_state" == "1" ]] || ctfctl_die "DYNAMIC_FLAG_FEATURE_CONFIG_INVALID" "dynamic instance flag configuration is invalid; secret values were not printed"
}

ctfctl_compose() {
    local release="$1"
    local env_file="$2"
    shift 2
    local -a compose_args=()
    mapfile -d '' -t compose_args < <(ctfctl_compose_args "$release" "$env_file")
    docker compose "${compose_args[@]}" "$@"
}

ctfctl_quiet_compose() {
    local release="$1"
    local env_file="$2"
    shift 2
    ctfctl_compose "$release" "$env_file" "$@" >/dev/null 2>&1
}

ctfctl_docker_is_running() {
    docker info >/dev/null 2>&1
}

ctfctl_assert_challenge_network_contract() {
    local driver
    local subnet

    # Docker network inspect is intentionally the only operation used here.
    # Callers such as preflight, deploy, and rollback must never take ownership
    # of an existing global network by deleting, recreating, or relabelling it.
    if ! docker network inspect "$CTFCTL_CHALLENGE_NETWORK_NAME" >/dev/null 2>&1; then
        ctfctl_die "CHALLENGE_NETWORK_MISSING" "ctf-challenges is missing; run the explicitly approved ctfctl install lifecycle first"
    fi

    driver="$(docker network inspect --format '{{.Driver}}' "$CTFCTL_CHALLENGE_NETWORK_NAME" 2>/dev/null || true)"
    [[ "$driver" == "$CTFCTL_CHALLENGE_NETWORK_DRIVER" ]] \
        || ctfctl_die "CHALLENGE_NETWORK_DRIVER_INVALID" "ctf-challenges must retain the reviewed bridge driver; the existing network was not changed"

    subnet="$(docker network inspect --format '{{range .IPAM.Config}}{{.Subnet}}{{"\n"}}{{end}}' "$CTFCTL_CHALLENGE_NETWORK_NAME" 2>/dev/null || true)"
    [[ "$subnet" == "$CTFCTL_CHALLENGE_NETWORK_SUBNET" ]] \
        || ctfctl_die "CHALLENGE_NETWORK_SUBNET_INVALID" "ctf-challenges must retain the reviewed 172.30.0.0/16 subnet; the existing network was not changed"

    ctfctl_ok "CHALLENGE_NETWORK_PRESENT" "ctf-challenges bridge network and reviewed subnet are present; labels were not modified"
}

ctfctl_assert_env_orchestrator_runtime_project_contract() {
    local container_id
    local project_label
    local service_label
    local found=0

    # A previously started Env container can survive outside the selected
    # Compose project.  That state is dangerous: it may use a different
    # dynamic-flag bind, Learn URL, or challenge network while appearing
    # healthy.  Inspect only; never remove, relabel, or restart a container
    # from this guard.
    while IFS= read -r container_id; do
        [[ -n "$container_id" ]] || continue
        found=1
        project_label="$(docker inspect --format '{{ index .Config.Labels "com.docker.compose.project" }}' "$container_id" 2>/dev/null || true)"
        service_label="$(docker inspect --format '{{ index .Config.Labels "com.docker.compose.service" }}' "$container_id" 2>/dev/null || true)"
        [[ "$project_label" == "$CTFCTL_PROJECT_NAME" ]] \
            || ctfctl_die "ENV_ORCHESTRATOR_PROJECT_DRIFT" "an env-orchestrator container belongs to a different Compose project; stop and review it before continuing"
        [[ "$service_label" == "env-orchestrator" ]] \
            || ctfctl_die "ENV_ORCHESTRATOR_SERVICE_LABEL_INVALID" "the discovered env-orchestrator container has an unexpected Compose service label"
    done < <(docker ps -a --no-trunc --filter 'label=com.docker.compose.service=env-orchestrator' --format '{{.ID}}' 2>/dev/null || ctfctl_die "ENV_ORCHESTRATOR_INSPECT_FAILED" "could not inspect existing env-orchestrator containers")

    if (( found == 0 )); then
        ctfctl_ok "ENV_ORCHESTRATOR_PROJECT_ABSENT" "no existing env-orchestrator container was found; runtime project drift is absent"
    else
        ctfctl_ok "ENV_ORCHESTRATOR_PROJECT_ALIGNED" "existing env-orchestrator container belongs to the fixed ctf_web Compose project"
    fi
}

ctfctl_ensure_challenge_network() {
    # This state-changing helper is called only by ctfctl install after its
    # root and exact-confirmation gates. An existing network is left intact;
    # after a create race we inspect and validate rather than retrying with a
    # destructive replacement.
    ctfctl_docker_is_running || ctfctl_die "DOCKER_UNAVAILABLE" "Docker daemon is not reachable for the approved challenge-network install"
    if ! docker network inspect "$CTFCTL_CHALLENGE_NETWORK_NAME" >/dev/null 2>&1; then
        if docker network create \
            --driver "$CTFCTL_CHALLENGE_NETWORK_DRIVER" \
            --subnet "$CTFCTL_CHALLENGE_NETWORK_SUBNET" \
            "$CTFCTL_CHALLENGE_NETWORK_NAME" >/dev/null 2>&1; then
            ctfctl_ok "CHALLENGE_NETWORK_CREATED" "ctf-challenges bridge network was created with the reviewed subnet"
        elif ! docker network inspect "$CTFCTL_CHALLENGE_NETWORK_NAME" >/dev/null 2>&1; then
            ctfctl_die "CHALLENGE_NETWORK_CREATE_FAILED" "could not create ctf-challenges bridge network; no replacement or deletion was attempted"
        fi
    fi

    ctfctl_assert_challenge_network_contract
}

ctfctl_compose_service_running() {
    local release="$1"
    local env_file="$2"
    local service="$3"
    local container_id
    container_id="$(ctfctl_compose "$release" "$env_file" ps -q "$service" 2>/dev/null || true)"
    [[ -n "$container_id" ]] || return 1
    [[ "$(docker inspect --format '{{.State.Running}}' "$container_id" 2>/dev/null || true)" == "true" ]]
}

ctfctl_require_service_declared() {
    local release="$1"
    local service="$2"
    grep -Eq "^  ${service}:$" "$release/infra/docker-compose.yml" || ctfctl_die "SERVICE_UNDECLARED" "release does not declare required service: ${service}"
}

ctfctl_require_service_running() {
    local release="$1"
    local env_file="$2"
    local service="$3"
    ctfctl_require_service_declared "$release" "$service"
    ctfctl_compose_service_running "$release" "$env_file" "$service" || ctfctl_die "SERVICE_NOT_RUNNING" "required service is not running: ${service}"
}

ctfctl_require_service_stopped() {
    local release="$1"
    local env_file="$2"
    local service="$3"
    ctfctl_require_service_declared "$release" "$service"
    if ctfctl_compose_service_running "$release" "$env_file" "$service"; then
        ctfctl_die "WRITER_SERVICE_RUNNING" "backup/restore requires the operator to stop writer service first: ${service}"
    fi
}

ctfctl_require_ctfd_upload_volume_attached() {
    local release="$1"
    local env_file="$2"
    local expected_volume="$3"
    local expected_mount="$4"
    local container_id
    local mounted_volumes

    ctfctl_require_service_declared "$release" "ctfd"
    container_id="$(ctfctl_compose "$release" "$env_file" ps --all --quiet ctfd 2>/dev/null || true)"
    [[ -n "$container_id" && "$container_id" != *$'\n'* ]] || ctfctl_die "CTFD_CONTAINER_UNRESOLVED" "cannot prove the CTFd upload volume is mounted by exactly one CTFd container"
    mounted_volumes="$(docker inspect --format '{{range .Mounts}}{{if eq .Type "volume"}}{{.Name}}:{{.Destination}}{{"\n"}}{{end}}{{end}}' "$container_id" 2>/dev/null || true)"
    printf '%s\n' "$mounted_volumes" | grep -Fqx "${expected_volume}:${expected_mount}" \
        || ctfctl_die "CTFD_UPLOAD_MOUNT_UNVERIFIED" "declared CTFd upload volume is not mounted at the approved destination"
}

ctfctl_known_volume_name() {
    local logical_name="$1"
    case "$logical_name" in
        redis_data|minio_data|caddy_data|caddy_config|ctfd_uploads)
            printf '%s_%s\n' "$CTFCTL_PROJECT_NAME" "$logical_name"
            ;;
        *)
            ctfctl_die "UNKNOWN_VOLUME" "volume is not in the approved persistent-volume allowlist"
            ;;
    esac
}

ctfctl_validate_contract_volume_name() {
    local volume="$1"
    [[ "$volume" =~ ^ctf_web_[a-z0-9_]+$ ]] || ctfctl_die "UNSAFE_CONTRACT_VOLUME" "CTFd upload volume must be a ctf_web named volume"
}

ctfctl_require_volume_exists() {
    local volume="$1"
    docker volume inspect "$volume" >/dev/null 2>&1 || ctfctl_die "VOLUME_MISSING" "required named volume is missing"
}

ctfctl_require_project_managed_volume() {
    local volume="$1"
    local project_label
    ctfctl_require_volume_exists "$volume"
    project_label="$(docker volume inspect --format '{{ index .Labels "com.docker.compose.project" }}' "$volume" 2>/dev/null || true)"
    [[ "$project_label" == "$CTFCTL_PROJECT_NAME" ]] || ctfctl_die "VOLUME_NOT_PROJECT_MANAGED" "named volume is not managed by the fixed ctf_web project"
}

ctfctl_archive_image_available() {
    docker image inspect "$CTFCTL_ARCHIVE_IMAGE" >/dev/null 2>&1 || ctfctl_die "ARCHIVE_IMAGE_MISSING" "archive helper image is unavailable; pull it during a separately approved maintenance step"
}

ctfctl_contract_value() {
    local contract_file="$1"
    local key="$2"
    local line
    line="$(grep -E "^[[:space:]]*${key}=" "$contract_file" 2>/dev/null | tail -n 1 || true)"
    [[ -n "$line" ]] || return 1
    line="${line#*=}"
    [[ "$line" =~ ^[A-Za-z0-9._-]+$ ]] || return 1
    printf '%s\n' "$line"
}

ctfctl_contract_csv_value() {
    local contract_file="$1"
    local key="$2"
    local line
    line="$(grep -E "^[[:space:]]*${key}=" "$contract_file" 2>/dev/null | tail -n 1 || true)"
    [[ -n "$line" ]] || return 1
    line="${line#*=}"
    [[ "$line" =~ ^[a-z0-9_]+(,[a-z0-9_]+)*$ ]] || return 1
    printf '%s\n' "$line"
}

ctfctl_contract_path_value() {
    local contract_file="$1"
    local key="$2"
    local line
    line="$(grep -E "^[[:space:]]*${key}=" "$contract_file" 2>/dev/null | tail -n 1 || true)"
    [[ -n "$line" ]] || return 1
    line="${line#*=}"
    ctfctl_is_safe_path_text "$line" || return 1
    printf '%s\n' "$line"
}

ctfctl_validate_backup_contract() {
    local release="$1"
    local env_file="$2"
    local contract_file
    local version
    local postgres_mode
    local ctfd_database
    local upload_mode
    local upload_volume
    local upload_mount
    local persistent_volume_names
    local expected_volume_names

    contract_file="$(ctfctl_resolve_existing_file "$CTFCTL_CONTRACT_FILE")"
    ctfctl_require_private_dir "$(dirname -- "$contract_file")" >/dev/null
    version="$(ctfctl_contract_value "$contract_file" "CONTRACT_VERSION" || true)"
    [[ "$version" == "$CTFCTL_BACKUP_CONTRACT_VERSION" ]] || ctfctl_die "BACKUP_CONTRACT_VERSION" "backup contract is absent or has an unsupported version"

    postgres_mode="$(ctfctl_contract_value "$contract_file" "POSTGRES_BACKUP_MODE" || true)"
    [[ "$postgres_mode" == "logical_pg_dumpall" ]] || ctfctl_die "POSTGRES_BACKUP_CONTRACT_MISSING" "PostgreSQL backup must use the declared logical pg_dumpall contract"

    ctfd_database="$(ctfctl_contract_value "$contract_file" "CTFD_DATABASE" || true)"
    [[ "$ctfd_database" =~ ^[a-z][a-z0-9_]{0,62}$ ]] || ctfctl_die "CTFD_DATABASE_CONTRACT_MISSING" "CTFd database name is missing or invalid in the backup contract"
    grep -Eq "@postgres:5432/${ctfd_database}([[:space:]]|$)" "$release/infra/docker-compose.yml" \
        || ctfctl_die "CTFD_DATABASE_COMPOSE_MISMATCH" "backup contract CTFd database does not match the release Compose declaration"

    upload_mode="$(ctfctl_contract_value "$contract_file" "CTFD_UPLOAD_MODE" || true)"
    [[ "$upload_mode" == "named_volume" ]] || ctfctl_die "CTFD_UPLOAD_CONTRACT_MISSING" "CTFd upload storage is not declared as a verified named volume; refusing an incomplete backup"

    upload_volume="$(ctfctl_contract_value "$contract_file" "CTFD_UPLOAD_VOLUME" || true)"
    [[ -n "$upload_volume" ]] || ctfctl_die "CTFD_UPLOAD_CONTRACT_MISSING" "CTFd upload volume is missing from the backup contract"
    ctfctl_validate_contract_volume_name "$upload_volume"
    [[ "$upload_volume" == "$(ctfctl_known_volume_name "ctfd_uploads")" ]] || ctfctl_die "CTFD_UPLOAD_CONTRACT_MISMATCH" "CTFd upload volume must use the fixed ctf_web_ctfd_uploads project volume"

    upload_mount="$(ctfctl_contract_path_value "$contract_file" "CTFD_UPLOAD_MOUNT" || true)"
    [[ "$upload_mount" == "/var/uploads" ]] || ctfctl_die "CTFD_UPLOAD_MOUNT_CONTRACT_MISSING" "CTFd upload mount must be declared as /var/uploads"

    persistent_volume_names="$(ctfctl_contract_csv_value "$contract_file" "PERSISTENT_VOLUME_LOGICAL_NAMES" || true)"
    expected_volume_names="$(IFS=,; printf '%s' "${CTFCTL_ARCHIVED_VOLUME_LOGICAL_NAMES[*]}")"
    [[ "$persistent_volume_names" == "$expected_volume_names" ]] || ctfctl_die "PERSISTENT_VOLUME_CONTRACT_MISMATCH" "backup contract must declare exactly the approved persistent volumes"

    ctfctl_assert_declared_persistent_volume_contract "$release"
    ctfctl_require_project_managed_volume "$upload_volume"
    ctfctl_require_ctfd_upload_volume_attached "$release" "$env_file" "$upload_volume" "$upload_mount"

    CTFCTL_CONTRACT_CTFD_DATABASE="$ctfd_database"
    CTFCTL_CONTRACT_CTFD_UPLOAD_VOLUME="$upload_volume"
    CTFCTL_CONTRACT_CTFD_UPLOAD_MOUNT="$upload_mount"
}

ctfctl_sha256_file() {
    local file="$1"
    sha256sum -- "$file" | awk '{print $1}'
}

ctfctl_write_manifest() {
    local bundle_dir="$1"
    local release="$2"
    shift 2
    local -a volumes=("$@")
    local timestamp
    local release_id
    local compose_hash
    local compose_prod_hash
    local ctfd_database="$CTFCTL_CONTRACT_CTFD_DATABASE"
    local ctfd_upload_volume="$CTFCTL_CONTRACT_CTFD_UPLOAD_VOLUME"
    local ctfd_upload_mount="$CTFCTL_CONTRACT_CTFD_UPLOAD_MOUNT"
    local volume
    local comma=""

    [[ "$ctfd_database" =~ ^[a-z][a-z0-9_]{0,62}$ ]] || ctfctl_die "BACKUP_CONTRACT_STATE_MISSING" "CTFd database backup contract was not loaded"
    [[ "$ctfd_upload_volume" == "$(ctfctl_known_volume_name "ctfd_uploads")" ]] || ctfctl_die "BACKUP_CONTRACT_STATE_MISSING" "CTFd upload backup contract was not loaded"
    [[ "$ctfd_upload_mount" == "/var/uploads" ]] || ctfctl_die "BACKUP_CONTRACT_STATE_MISSING" "CTFd upload mount backup contract was not loaded"

    timestamp="$(date -u '+%Y-%m-%dT%H:%M:%SZ')"
    release_id="$(ctfctl_release_id "$release")"
    compose_hash="$(ctfctl_sha256_file "$release/infra/docker-compose.yml")"
    compose_prod_hash="$(ctfctl_sha256_file "$release/infra/docker-compose.prod.yml")"

    {
        printf '{\n'
        printf '  "schema_version": %s,\n' "$CTFCTL_MANIFEST_SCHEMA"
        printf '  "bundle_id": "%s",\n' "$(basename -- "$bundle_dir")"
        printf '  "created_at_utc": "%s",\n' "$timestamp"
        printf '  "project": "%s",\n' "$CTFCTL_PROJECT_NAME"
        printf '  "completeness": "complete",\n'
        printf '  "source_release_id": "%s",\n' "$release_id"
        printf '  "compose_sha256": "%s",\n' "$compose_hash"
        printf '  "compose_prod_sha256": "%s",\n' "$compose_prod_hash"
        printf '  "database_dump": "postgres/all-databases.sql",\n'
        printf '  "ctfd_database": "%s",\n' "$ctfd_database"
        printf '  "ctfd_upload_volume": "%s",\n' "$ctfd_upload_volume"
        printf '  "ctfd_upload_mount": "%s",\n' "$ctfd_upload_mount"
        printf '  "volume_archives": ['
        for volume in "${volumes[@]}"; do
            printf '%s"volumes/%s.tar"' "$comma" "$volume"
            comma=", "
        done
        printf ']\n'
        printf '}\n'
    } > "$bundle_dir/manifest.json"
}

ctfctl_write_checksums() {
    local bundle_dir="$1"
    (
        cd -- "$bundle_dir"
        find postgres volumes -type f -print0 | LC_ALL=C sort -z | xargs -0 sha256sum > checksums.sha256
    )
}

ctfctl_database_dump_contains() {
    local dump_file="$1"
    local database="$2"

    [[ "$database" =~ ^[a-z][a-z0-9_]{0,62}$ ]] || return 1
    grep -Eq "^CREATE DATABASE[[:space:]]+\\\"?${database}\\\"?([[:space:];]|$)" "$dump_file"
}

ctfctl_assert_database_dump_contains() {
    local dump_file="$1"
    local database="$2"

    ctfctl_database_dump_contains "$dump_file" "$database" \
        || ctfctl_die "CTFD_DATABASE_DUMP_MISSING" "PostgreSQL logical dump does not prove that the declared CTFd database is included"
}

ctfctl_verify_bundle() {
    local bundle_dir="$1"
    local bundle_id
    local required_file
    local manifest_file
    local checksums_file
    local ctfd_database
    local ctfd_upload_volume
    local ctfd_upload_mount

    bundle_dir="$(ctfctl_require_private_dir "$bundle_dir")"
    ctfctl_require_private_dir "$(dirname -- "$bundle_dir")" >/dev/null
    bundle_id="$(basename -- "$bundle_dir")"
    ctfctl_require_safe_id "$bundle_id" "bundle id"
    manifest_file="$(ctfctl_resolve_existing_file "$bundle_dir/manifest.json")"
    checksums_file="$(ctfctl_resolve_existing_file "$bundle_dir/checksums.sha256")"
    grep -Fq '"schema_version": 1' "$manifest_file" || ctfctl_die "BUNDLE_SCHEMA_UNSUPPORTED" "bundle manifest schema is unsupported"
    grep -Fq '"project": "ctf_web"' "$manifest_file" || ctfctl_die "BUNDLE_PROJECT_MISMATCH" "bundle project does not match ctf_web"
    grep -Fq '"completeness": "complete"' "$manifest_file" || ctfctl_die "BUNDLE_INCOMPLETE" "bundle does not declare a complete persistent-data backup"
    required_file="$bundle_dir/postgres/all-databases.sql"
    [[ -s "$required_file" ]] || ctfctl_die "DATABASE_DUMP_MISSING" "bundle PostgreSQL logical dump is missing or empty"
    ctfd_database="$(ctfctl_json_string_value "$manifest_file" "ctfd_database" || true)"
    ctfd_upload_volume="$(ctfctl_json_string_value "$manifest_file" "ctfd_upload_volume" || true)"
    ctfd_upload_mount="$(ctfctl_json_string_value "$manifest_file" "ctfd_upload_mount" || true)"
    [[ "$ctfd_upload_volume" == "$(ctfctl_known_volume_name "ctfd_uploads")" ]] || ctfctl_die "BUNDLE_CTFD_UPLOAD_VOLUME_INVALID" "bundle does not bind the approved CTFd upload volume"
    [[ "$ctfd_upload_mount" == "/var/uploads" ]] || ctfctl_die "BUNDLE_CTFD_UPLOAD_MOUNT_INVALID" "bundle does not bind the approved CTFd upload mount"
    grep -Fq "\"volumes/${ctfd_upload_volume}.tar\"" "$manifest_file" \
        || ctfctl_die "BUNDLE_CTFD_UPLOAD_ARCHIVE_MISSING" "bundle manifest does not include the CTFd upload archive"
    ctfctl_assert_database_dump_contains "$required_file" "$ctfd_database"
    (
        cd -- "$bundle_dir"
        sha256sum --check --status "$(basename -- "$checksums_file")"
    ) || ctfctl_die "BUNDLE_CHECKSUM_FAILED" "bundle checksum validation failed"
    printf '%s\n' "$bundle_dir"
}

ctfctl_bundle_id_matches_confirmation() {
    local bundle_dir="$1"
    local confirmation="$2"
    [[ "$(basename -- "$bundle_dir")" == "$confirmation" ]] || ctfctl_die "CONFIRMATION_MISMATCH" "confirmation must exactly match the resolved bundle id"
}

ctfctl_release_id_matches_confirmation() {
    local release="$1"
    local confirmation="$2"
    [[ "$(ctfctl_release_id "$release")" == "$confirmation" ]] || ctfctl_die "CONFIRMATION_MISMATCH" "confirmation must exactly match the resolved release id"
}

ctfctl_archive_volume() {
    local volume="$1"
    local bundle_dir="$2"
    local archive_path="/backup/volumes/${volume}.tar"

    ctfctl_require_project_managed_volume "$volume"
    docker run --rm --pull=never --read-only --network none --cap-drop ALL \
        --security-opt no-new-privileges \
        --user 0:0 \
        -v "${volume}:/source:ro" \
        -v "${bundle_dir}:/backup" \
        "$CTFCTL_ARCHIVE_IMAGE" \
        sh -ec "tar -C /source -cf '${archive_path}' ." >/dev/null 2>&1 \
        || ctfctl_die "VOLUME_ARCHIVE_FAILED" "could not archive an approved named volume"
}

ctfctl_restore_volume() {
    local volume="$1"
    local bundle_dir="$2"
    local archive_name="${volume}.tar"
    local archive_path="$bundle_dir/volumes/$archive_name"

    [[ -s "$archive_path" ]] || ctfctl_die "VOLUME_ARCHIVE_MISSING" "bundle is missing an expected volume archive"
    ctfctl_require_project_managed_volume "$volume"

    # A bundle may only restore entries below the mounted volume root. The
    # archives created by ctfctl use relative ./ paths; reject absolute or
    # parent-traversal entries before mounting the target read/write.
    docker run --rm --pull=never --read-only --network none --cap-drop ALL \
        --security-opt no-new-privileges \
        --user 0:0 \
        -v "${bundle_dir}:/backup:ro" \
        "$CTFCTL_ARCHIVE_IMAGE" \
        sh -ec "! tar -tf '/backup/volumes/${archive_name}' | grep -Eq '(^/|(^|/)\\.\\.(/|$))'" >/dev/null 2>&1 \
        || ctfctl_die "VOLUME_ARCHIVE_UNSAFE" "bundle volume archive contains an unsafe path"

    docker run --rm --pull=never --read-only --network none --cap-drop ALL \
        --security-opt no-new-privileges \
        --user 0:0 \
        -v "${volume}:/target" \
        -v "${bundle_dir}:/backup:ro" \
        "$CTFCTL_ARCHIVE_IMAGE" \
        sh -ec "find /target -mindepth 1 -maxdepth 1 -exec rm -rf -- '{}' + && tar -C /target -xf '/backup/volumes/${archive_name}'" >/dev/null 2>&1 \
        || ctfctl_die "VOLUME_RESTORE_FAILED" "could not restore an approved named volume; manual recovery review is required"
}

ctfctl_require_ctfd_runtime_migration_evidence() {
    # The local CTFd wrapper does not declare its own ENTRYPOINT, while Compose
    # supplies only a runtime command. The exact behavior of the inherited
    # upstream entrypoint is not part of this release contract. Until an
    # immutable-image proof, a one-shot migration contract, a non-migrating
    # runtime contract, and verified restore/rollback evidence are all bound to
    # the release, do not start a CTFd runtime from any state-changing command.
    # This intentionally has no bypass flag: accepting an operator-supplied
    # command would turn missing evidence into an unreviewed migration path.
    ctfctl_die "CTFD_MIGRATION_EVIDENCE_REQUIRED" "CTFd migration, runtime no-reexecution, and restore/rollback evidence are not yet bound to this release; no CTFd runtime was started"
}

ctfctl_assert_full_migration_contract() {
    local release="$1"
    local service
    local -a required_jobs=("auth-migrate" "learn-migrate" "community-migrate")

    for service in "${required_jobs[@]}"; do
        grep -Eq "^  ${service}:$" "$release/infra/docker-compose.yml" || ctfctl_die "MIGRATION_CONTRACT_INCOMPLETE" "release has no declared versioned migration job for ${service}; refusing schema changes"
    done

    ctfctl_require_ctfd_runtime_migration_evidence
}

ctfctl_assert_immutable_image_contract() {
    local release="$1"
    local compose_file
    local image_count

    # A production release may not rebuild source images on the server. Each
    # image declared by the base Compose file must be immutable; a production
    # override may add image declarations but may not weaken that rule.
    for compose_file in "$release/infra/docker-compose.yml" "$release/infra/docker-compose.prod.yml"; do
        grep -Eq '^[[:space:]]+build:' "$compose_file" \
            && ctfctl_die "IMMUTABLE_IMAGE_CONTRACT_MISSING" "release contains build directives; refusing a server-side source rebuild"
        if grep -E '^[[:space:]]+image:' "$compose_file" | grep -Ev '^[[:space:]]+image:[[:space:]]+[^[:space:]#]+@sha256:[0-9a-f]{64}([[:space:]]+#.*)?$' >/dev/null; then
            ctfctl_die "IMAGE_DIGESTS_MISSING" "release contains an image that is not digest-pinned"
        fi
    done
    image_count="$(grep -Ec '^[[:space:]]+image:' "$release/infra/docker-compose.yml" || true)"
    (( image_count > 0 )) || ctfctl_die "IMAGE_DIGESTS_MISSING" "release does not declare any immutable service images"
}

ctfctl_write_migration_receipt() {
    local release="$1"
    local bundle_dir="$2"
    local receipt
    local state_dir
    local migrations_dir
    ctfctl_require_state_dir_scope
    state_dir="$(ctfctl_require_private_dir "$CTFCTL_STATE_DIR")"
    migrations_dir="$state_dir/migrations"
    ctfctl_create_private_dir_if_missing "$migrations_dir"
    receipt="$migrations_dir/$(ctfctl_release_id "$release").ok"
    if [[ -e "$receipt" || -L "$receipt" ]]; then
        [[ -f "$receipt" && ! -L "$receipt" ]] || ctfctl_die "MIGRATION_RECEIPT_INVALID" "migration receipt target is not a regular file"
    fi
    printf 'release_id=%s\nbackup_bundle_id=%s\ncompleted_at_utc=%s\n' \
        "$(ctfctl_release_id "$release")" \
        "$(basename -- "$bundle_dir")" \
        "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" > "$receipt"
}

ctfctl_require_migration_receipt() {
    local release="$1"
    local bundle_dir="$2"
    local receipt
    local state_dir
    ctfctl_require_state_dir_scope
    state_dir="$(ctfctl_require_private_dir "$CTFCTL_STATE_DIR")"
    receipt="$state_dir/migrations/$(ctfctl_release_id "$release").ok"
    [[ -f "$receipt" ]] || ctfctl_die "MIGRATION_RECEIPT_MISSING" "run the validated migrate command for this release before deploy"
    grep -Fqx "release_id=$(ctfctl_release_id "$release")" "$receipt" || ctfctl_die "MIGRATION_RECEIPT_INVALID" "migration receipt does not match the requested release"
    grep -Fqx "backup_bundle_id=$(basename -- "$bundle_dir")" "$receipt" || ctfctl_die "MIGRATION_RECEIPT_INVALID" "migration receipt does not bind the supplied backup bundle"
}

ctfctl_activate_release() {
    local release="$1"
    local link_parent
    local temporary_link

    ctfctl_require_safe_absolute_path "$CTFCTL_CURRENT_LINK"
    link_parent="$(ctfctl_require_private_dir "$(dirname -- "$CTFCTL_CURRENT_LINK")")"
    ctfctl_is_descendant "$CTFCTL_CURRENT_LINK" "/opt/ctf-web" || ctfctl_die "CURRENT_LINK_OUTSIDE_ROOT" "current release link must stay under /opt/ctf-web"
    temporary_link="$link_parent/.ctfctl-current-$(ctfctl_release_id "$release")-$$"
    [[ ! -e "$temporary_link" && ! -L "$temporary_link" ]] || ctfctl_die "TEMPORARY_LINK_EXISTS" "temporary release link already exists"
    ln -s -- "$release" "$temporary_link"
    mv -Tf -- "$temporary_link" "$CTFCTL_CURRENT_LINK"
}

ctfctl_require_rollback_marker() {
    local release="$1"
    local marker="$release/.ctfctl-rollback-safe"
    [[ -f "$marker" ]] || ctfctl_die "ROLLBACK_CONTRACT_MISSING" "release lacks a reviewed rollback-compatibility marker; refusing a code/database rollback"
    marker="$(ctfctl_require_immutable_release_file "$marker")"
    grep -Fqx "release_id=$(ctfctl_release_id "$release")" "$marker" || ctfctl_die "ROLLBACK_CONTRACT_INVALID" "rollback marker does not match the release id"
    grep -Fqx "database_backward_compatible=true" "$marker" || ctfctl_die "ROLLBACK_CONTRACT_INVALID" "rollback marker does not attest database backward compatibility"
}

ctfctl_preflight() {
    local requested_release="$1"
    local release
    local env_file
    local port
    local listening=0
    local -a required_commands=("docker" "realpath" "stat" "df" "ss" "grep" "sha256sum" "awk")
    local command_name

    ctfctl_require_linux
    for command_name in "${required_commands[@]}"; do
        ctfctl_require_command "$command_name"
    done
    ctfctl_docker_is_running || ctfctl_die "DOCKER_UNAVAILABLE" "Docker daemon is not reachable by the current user"
    docker compose version >/dev/null 2>&1 || ctfctl_die "COMPOSE_UNAVAILABLE" "Docker Compose v2 is not available"
    docker version --format '{{.Server.Version}}' >/dev/null 2>&1 || ctfctl_die "DOCKER_VERSION_UNREADABLE" "Docker Engine version cannot be read"
    docker compose version --short >/dev/null 2>&1 || ctfctl_die "COMPOSE_VERSION_UNREADABLE" "Docker Compose version cannot be read"
    ctfctl_assert_env_orchestrator_runtime_project_contract

    release="$(ctfctl_validate_release "$requested_release")"
    env_file="$(ctfctl_require_env_file)"
    ctfctl_assert_dynamic_flag_start_contract "$env_file"
    ctfctl_assert_release_manifest_contract "$release"
    ctfctl_assert_education_private_storage_contract "$release"
    ctfctl_assert_env_orchestrator_build_context_host_contract "$release"
    ctfctl_assert_education_private_bucket_anonymous_contract "$release" "$env_file"
    ctfctl_assert_immutable_image_contract "$release"
    ctfctl_assert_declared_persistent_volume_contract "$release"
    ctfctl_assert_full_migration_contract "$release"

    ctfctl_info "PREFLIGHT_RELEASE" "release path accepted"
    ctfctl_info "PREFLIGHT_DOCKER" "Docker Engine and Compose are reachable and version metadata is readable"
    df -Pk "$release" >/dev/null 2>&1 || ctfctl_die "DISK_CHECK_FAILED" "could not inspect release filesystem capacity"
    df -Pi "$release" >/dev/null 2>&1 || ctfctl_die "INODE_CHECK_FAILED" "could not inspect release filesystem inode capacity"
    ctfctl_ok "PREFLIGHT_STORAGE" "filesystem capacity and inode counters are readable"

    for port in 80 443 8080 5432 6379 9000 9001 8010 8020 8100 8200 8300 3100; do
        if ss -H -ltn "sport = :${port}" 2>/dev/null | grep -q .; then
            ctfctl_warn "PORT_IN_USE" "port ${port} has a listener; compare it with the intended deployment before proceeding"
            listening=1
        else
            ctfctl_ok "PORT_AVAILABLE" "port ${port} has no TCP listener"
        fi
    done
    (( listening == 0 )) || ctfctl_warn "PORT_REVIEW_REQUIRED" "one or more inspected ports are already in use"

    ctfctl_assert_challenge_network_contract

    if ctfctl_quiet_compose "$release" "$env_file" config --no-interpolate -q; then
        ctfctl_ok "COMPOSE_CONFIG_VALID" "Compose syntax is valid without printing interpolated values"
    else
        ctfctl_die "COMPOSE_CONFIG_INVALID" "Compose configuration could not be validated without exposing environment values"
    fi

    ctfctl_check_secret_references "$env_file" || ctfctl_die "SECRET_REFERENCE_CHECK_FAILED" "required secret/config references are incomplete; values were not printed"
    if docker image inspect "$CTFCTL_ARCHIVE_IMAGE" >/dev/null 2>&1; then
        ctfctl_ok "ARCHIVE_IMAGE_PRESENT" "offline archive helper image is present"
    else
        ctfctl_warn "ARCHIVE_IMAGE_MISSING" "offline archive helper image is not present; backup/restore will refuse until an approved image-pull step"
    fi

    ctfctl_ok "PREFLIGHT_COMPLETE" "read-only checks completed; VMware and production smoke tests have not been executed by this command"
}

ctfctl_backup() {
    local output_path="$1"
    local release
    local env_file
    local bundle_dir
    local logical_name
    local volume
    local -a volumes=()

    ctfctl_require_linux
    ctfctl_require_command "docker"
    ctfctl_require_command "realpath"
    ctfctl_require_command "stat"
    ctfctl_require_command "grep"
    ctfctl_require_command "sha256sum"
    ctfctl_require_command "awk"
    ctfctl_require_command "find"
    ctfctl_require_command "xargs"
    ctfctl_require_command "sort"
    ctfctl_require_command "mkdir"
    ctfctl_docker_is_running || ctfctl_die "DOCKER_UNAVAILABLE" "Docker daemon is not reachable by the current user"
    ctfctl_assert_env_orchestrator_runtime_project_contract
    release="$(ctfctl_active_release)"
    env_file="$(ctfctl_require_env_file)"
    ctfctl_assert_dynamic_flag_start_contract "$env_file"
    ctfctl_require_safe_absolute_path "$output_path"
    [[ "$(basename -- "$output_path")" =~ ^ctf-web-backup-[A-Za-z0-9._-]+$ ]] || ctfctl_die "BUNDLE_ID_INVALID" "backup directory basename must start with ctf-web-backup-"
    bundle_dir="$(ctfctl_resolve_new_path "$output_path")"

    # We never stop writer services automatically. A logical PostgreSQL dump is
    # consistent while PostgreSQL runs, while named volume archives need their
    # writers quiesced by the operator before this command starts.
    ctfctl_require_service_running "$release" "$env_file" "postgres"
    for logical_name in "ctfd" "auth-service" "learn-service" "community-service" "env-orchestrator" "minio" "redis" "caddy"; do
        ctfctl_require_service_stopped "$release" "$env_file" "$logical_name"
    done
    ctfctl_archive_image_available
    ctfctl_validate_backup_contract "$release" "$env_file"
    for logical_name in "${CTFCTL_ARCHIVED_VOLUME_LOGICAL_NAMES[@]}"; do
        volume="$(ctfctl_known_volume_name "$logical_name")"
        ctfctl_require_project_managed_volume "$volume"
        volumes+=("$volume")
    done

    mkdir -m 0700 -- "$bundle_dir"
    mkdir -m 0700 -- "$bundle_dir/postgres" "$bundle_dir/volumes"
    if ! ctfctl_compose "$release" "$env_file" exec -T postgres sh -ec 'pg_dumpall -U "$POSTGRES_USER" --clean --if-exists --no-owner --no-privileges' > "$bundle_dir/postgres/all-databases.sql" 2>/dev/null; then
        rm -rf -- "$bundle_dir"
        ctfctl_die "POSTGRES_DUMP_FAILED" "logical PostgreSQL dump failed; no backup bundle was retained"
    fi
    [[ -s "$bundle_dir/postgres/all-databases.sql" ]] || { rm -rf -- "$bundle_dir"; ctfctl_die "POSTGRES_DUMP_EMPTY" "logical PostgreSQL dump was empty; no backup bundle was retained"; }
    if ! ctfctl_database_dump_contains "$bundle_dir/postgres/all-databases.sql" "$CTFCTL_CONTRACT_CTFD_DATABASE"; then
        rm -rf -- "$bundle_dir"
        ctfctl_die "CTFD_DATABASE_DUMP_MISSING" "PostgreSQL logical dump does not prove that the declared CTFd database is included; no backup bundle was retained"
    fi

    for volume in "${volumes[@]}"; do
        ctfctl_archive_volume "$volume" "$bundle_dir"
    done
    ctfctl_write_checksums "$bundle_dir"
    ctfctl_write_manifest "$bundle_dir" "$release" "${volumes[@]}"
    ctfctl_ok "BACKUP_COMPLETE" "complete bundle created with PostgreSQL logical dump, approved named-volume archives, manifest, and checksums"
}

ctfctl_restore() {
    local requested_bundle="$1"
    local confirmation="$2"
    local release
    local env_file
    local bundle_dir
    local archive_entry
    local volume
    local -a volumes=()
    local -a expected_volumes=()

    ctfctl_require_linux
    ctfctl_require_command "docker"
    ctfctl_require_command "realpath"
    ctfctl_require_command "stat"
    ctfctl_require_command "grep"
    ctfctl_require_command "sort"
    ctfctl_require_command "sha256sum"
    ctfctl_require_command "awk"
    ctfctl_docker_is_running || ctfctl_die "DOCKER_UNAVAILABLE" "Docker daemon is not reachable by the current user"
    bundle_dir="$(ctfctl_verify_bundle "$requested_bundle")"
    ctfctl_bundle_id_matches_confirmation "$bundle_dir" "$confirmation"
    release="$(ctfctl_active_release)"
    env_file="$(ctfctl_require_env_file)"
    ctfctl_assert_dynamic_flag_start_contract "$env_file"
    ctfctl_archive_image_available
    ctfctl_validate_backup_contract "$release" "$env_file"
    for logical_name in "${CTFCTL_ARCHIVED_VOLUME_LOGICAL_NAMES[@]}"; do
        expected_volumes+=("$(ctfctl_known_volume_name "$logical_name")")
    done

    # The target stack must already be deliberately quiesced. ctfctl never uses
    # down -v and never stops unrelated processes or containers on its own.
    ctfctl_require_service_stopped "$release" "$env_file" "redis"
    ctfctl_require_service_stopped "$release" "$env_file" "minio"
    ctfctl_require_service_stopped "$release" "$env_file" "caddy"
    ctfctl_require_service_stopped "$release" "$env_file" "ctfd"
    ctfctl_require_service_stopped "$release" "$env_file" "auth-service"
    ctfctl_require_service_stopped "$release" "$env_file" "learn-service"
    ctfctl_require_service_stopped "$release" "$env_file" "community-service"
    ctfctl_require_service_stopped "$release" "$env_file" "env-orchestrator"
    ctfctl_require_service_running "$release" "$env_file" "postgres"

    while IFS= read -r archive_entry; do
        volume="${archive_entry#*volumes/}"
        volume="${volume%.tar*}"
        [[ "$volume" =~ ^ctf_web_[a-z0-9_]+$ ]] || ctfctl_die "BUNDLE_VOLUME_UNSAFE" "bundle requests a volume outside the ctf_web allowlist"
        volumes+=("$volume")
    done < <(grep -o 'volumes/ctf_web_[a-z0-9_]*\.tar' "$bundle_dir/manifest.json" | sort -u)
    (( ${#volumes[@]} == ${#expected_volumes[@]} )) || ctfctl_die "BUNDLE_VOLUME_SET_INVALID" "bundle volume set does not exactly match the approved persistent-data contract"
    for volume in "${expected_volumes[@]}"; do
        printf '%s\n' "${volumes[@]}" | grep -Fqx "$volume" || ctfctl_die "BUNDLE_VOLUME_SET_INVALID" "bundle volume set does not exactly match the approved persistent-data contract"
    done

    # Restoring a logical dump is intentionally last: if an archive is missing
    # or corrupt, the database remains untouched. This is still an overwrite
    # operation; the exact bundle-id confirmation above is mandatory.
    for volume in "${volumes[@]}"; do
        ctfctl_restore_volume "$volume" "$bundle_dir"
    done
    if ! ctfctl_compose "$release" "$env_file" exec -T postgres sh -ec 'psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d postgres' < "$bundle_dir/postgres/all-databases.sql" >/dev/null 2>&1; then
        ctfctl_die "POSTGRES_RESTORE_FAILED" "PostgreSQL restore failed after volume replacement; keep services stopped and perform manual recovery review"
    fi
    ctfctl_ok "RESTORE_COMPLETE" "bundle restore command completed; start services only after a separate smoke test"
}

ctfctl_migrate() {
    local requested_release="$1"
    local requested_bundle="$2"
    local confirmation="$3"
    local release
    local env_file
    local bundle_dir
    local job
    local expected_confirmation

    ctfctl_require_linux
    ctfctl_require_command "docker"
    ctfctl_require_command "realpath"
    ctfctl_require_command "stat"
    ctfctl_require_command "grep"
    ctfctl_require_command "mkdir"
    ctfctl_require_command "sha256sum"
    ctfctl_require_command "awk"
    ctfctl_docker_is_running || ctfctl_die "DOCKER_UNAVAILABLE" "Docker daemon is not reachable by the current user"
    ctfctl_assert_env_orchestrator_runtime_project_contract
    release="$(ctfctl_validate_release "$requested_release")"
    bundle_dir="$(ctfctl_verify_bundle "$requested_bundle")"
    expected_confirmation="migrate:$(ctfctl_release_id "$release"):$(basename -- "$bundle_dir")"
    [[ "$confirmation" == "$expected_confirmation" ]] || ctfctl_die "CONFIRMATION_MISMATCH" "migrate confirmation must bind the exact release and backup bundle"
    env_file="$(ctfctl_require_env_file)"
    ctfctl_assert_dynamic_flag_start_contract "$env_file"
    ctfctl_assert_release_manifest_contract "$release"
    ctfctl_assert_education_private_storage_contract "$release"
    ctfctl_assert_immutable_image_contract "$release"
    ctfctl_assert_declared_persistent_volume_contract "$release"
    ctfctl_assert_full_migration_contract "$release"
    ctfctl_require_service_running "$release" "$env_file" "postgres"

    for job in auth-migrate learn-migrate community-migrate; do
        ctfctl_quiet_compose "$release" "$env_file" run --rm --no-deps "$job" || ctfctl_die "MIGRATION_JOB_FAILED" "versioned migration job failed: ${job}"
    done
    ctfctl_write_migration_receipt "$release" "$bundle_dir"
    ctfctl_ok "MIGRATE_COMPLETE" "all declared versioned migration jobs completed after backup validation"
}

ctfctl_deploy() {
    local requested_release="$1"
    local requested_bundle="$2"
    local confirmation="$3"
    local release
    local env_file
    local bundle_dir
    local expected_confirmation

    ctfctl_require_linux
    ctfctl_require_command "docker"
    ctfctl_require_command "realpath"
    ctfctl_require_command "stat"
    ctfctl_require_command "grep"
    ctfctl_require_command "sha256sum"
    ctfctl_require_command "awk"
    ctfctl_docker_is_running || ctfctl_die "DOCKER_UNAVAILABLE" "Docker daemon is not reachable by the current user"
    ctfctl_assert_env_orchestrator_runtime_project_contract
    release="$(ctfctl_validate_release "$requested_release")"
    bundle_dir="$(ctfctl_verify_bundle "$requested_bundle")"
    expected_confirmation="deploy:$(ctfctl_release_id "$release"):$(basename -- "$bundle_dir")"
    [[ "$confirmation" == "$expected_confirmation" ]] || ctfctl_die "CONFIRMATION_MISMATCH" "deploy confirmation must bind the exact release and backup bundle"
    env_file="$(ctfctl_require_env_file)"
    ctfctl_assert_dynamic_flag_start_contract "$env_file"
    ctfctl_assert_release_manifest_contract "$release"
    ctfctl_assert_education_private_storage_contract "$release"
    ctfctl_assert_env_orchestrator_build_context_host_contract "$release"
    ctfctl_assert_immutable_image_contract "$release"
    ctfctl_assert_declared_persistent_volume_contract "$release"
    ctfctl_assert_full_migration_contract "$release"
    ctfctl_require_migration_receipt "$release" "$bundle_dir"
    # deploy only validates the lifecycle resource prepared by the explicit
    # install command. It never creates a network while changing a release.
    ctfctl_assert_challenge_network_contract

    # Run from the selected immutable release and do not build on the server.
    # The pointer flips only after Compose reports success. If Compose itself
    # fails, the pointer stays unchanged and the operator receives a non-zero
    # result for manual recovery review.
    ctfctl_quiet_compose "$release" "$env_file" up -d --no-build || ctfctl_die "DEPLOY_COMPOSE_FAILED" "Compose could not start the selected release; current release pointer was not changed"
    ctfctl_activate_release "$release"
    ctfctl_ok "DEPLOY_COMPLETE" "selected immutable release is active; run smoke-test separately before declaring verification complete"
}

ctfctl_rollback() {
    local requested_release="$1"
    local requested_restore_bundle="$2"
    local confirmation="$3"
    local release
    local env_file
    local expected_confirmation
    local bundle_dir=""

    ctfctl_require_linux
    ctfctl_require_command "docker"
    ctfctl_require_command "realpath"
    ctfctl_require_command "stat"
    ctfctl_require_command "grep"
    ctfctl_require_command "sha256sum"
    ctfctl_require_command "awk"
    ctfctl_docker_is_running || ctfctl_die "DOCKER_UNAVAILABLE" "Docker daemon is not reachable by the current user"
    ctfctl_assert_env_orchestrator_runtime_project_contract
    release="$(ctfctl_validate_release "$requested_release")"
    ctfctl_require_rollback_marker "$release"
    ctfctl_assert_release_manifest_contract "$release"
    ctfctl_assert_education_private_storage_contract "$release"
    ctfctl_assert_env_orchestrator_build_context_host_contract "$release"
    ctfctl_assert_immutable_image_contract "$release"
    ctfctl_assert_declared_persistent_volume_contract "$release"
    # rollback can start the production Compose stack, so it uses the same
    # read-only network gate as deploy rather than creating any resource.
    ctfctl_assert_challenge_network_contract
    env_file="$(ctfctl_require_env_file)"
    ctfctl_assert_dynamic_flag_start_contract "$env_file"
    # `rollback` may otherwise restore persistent CTFd data and then start the
    # inherited CTFd runtime through Compose. Keep this gate before both the
    # optional restore and `compose up` until the runtime contract is proven.
    ctfctl_require_ctfd_runtime_migration_evidence

    if [[ -n "$requested_restore_bundle" ]]; then
        bundle_dir="$(ctfctl_verify_bundle "$requested_restore_bundle")"
        expected_confirmation="rollback:$(ctfctl_release_id "$release"):$(basename -- "$bundle_dir")"
    else
        expected_confirmation="rollback:$(ctfctl_release_id "$release")"
    fi
    [[ "$confirmation" == "$expected_confirmation" ]] || ctfctl_die "CONFIRMATION_MISMATCH" "rollback confirmation does not bind the exact release and optional bundle"

    # Database restore remains explicit and inherits its own exact bundle-id
    # validation. The caller has already supplied the compound rollback
    # confirmation above, so no guessed backup path is ever used.
    if [[ -n "$bundle_dir" ]]; then
        ctfctl_restore "$bundle_dir" "$(basename -- "$bundle_dir")"
    fi

    ctfctl_quiet_compose "$release" "$env_file" up -d --no-build || ctfctl_die "ROLLBACK_COMPOSE_FAILED" "Compose could not start the rollback release; current release pointer was not changed"
    ctfctl_activate_release "$release"
    ctfctl_ok "ROLLBACK_COMPLETE" "rollback release is active; run smoke-test and data-integrity checks separately"
}

ctfctl_smoke_test() {
    local base_url="$1"
    local endpoint
    local search_type=""
    local search_id=""
    local -a endpoints=()

    ctfctl_require_command "curl"
    ctfctl_require_command "python3"
    [[ "$base_url" =~ ^https://[A-Za-z0-9.-]+(:[0-9]{1,5})?(/ctf)?$ ]] || ctfctl_die "BASE_URL_INVALID" "base URL must be https://host or https://host/ctf without query, fragment, or credentials"
    if [[ "$base_url" == */ctf ]]; then
        base_url="${base_url%/ctf}"
    fi
    endpoints=(
        "/health"
        "/ctf"
        "/ctf/ko"
        "/ctf/ko/learn?view=map"
        "/ctf/api/learn/knowledge-map/v2/overview"
        "/ctf/api/learn/knowledge-map/v2/canvas"
        "/ctf/api/learn/knowledge-map/v2/search?q=course&limit=1"
    )
    for endpoint in "${endpoints[@]}"; do
        local response
        response="$(curl --fail --silent --show-error --location --max-time 15 --proto '=https' --tlsv1.2 "$base_url$endpoint")" \
            || ctfctl_die "SMOKE_HTTP_FAILED" "HTTP smoke probe failed for a public route"
        if [[ "$endpoint" == "/ctf/ko/learn?view=map" ]]; then
            # HTTP checks the initial route only. The canvas loads through a
            # client effect and must be accepted in an actual browser.
            printf '%s' "$response" | grep -Eq '<button[^>]*id="learn-map-tab"[^>]*aria-selected="true"' \
                && [[ "$response" == *'aria-controls="learn-map-panel"'* ]] \
                && [[ "$response" == *'id="learn-map-panel"'* ]] \
                || ctfctl_die "SMOKE_GRAPH_UI_CONTRACT_FAILED" "public learn route did not select the map panel"
        fi
        if [[ "$endpoint" == "/ctf/api/learn/knowledge-map/v2/overview" ]]; then
            printf '%s' "$response" | grep -Eq '"graph_revision"[[:space:]]*:[[:space:]]*"[^"]+"' \
                && printf '%s' "$response" | grep -Eq '"mapping_coverage"[[:space:]]*:[[:space:]]*\{' \
                && printf '%s' "$response" | grep -Eq '"node_counts"[[:space:]]*:[[:space:]]*\{' \
                && printf '%s' "$response" | grep -Eq '"edge_counts"[[:space:]]*:[[:space:]]*\{' \
                && printf '%s' "$response" | grep -Eq '"relation_count"[[:space:]]*:[[:space:]]*[0-9]+' \
                && printf '%s' "$response" | grep -Eq '"challenge"[[:space:]]*:[[:space:]]*[0-9]+' \
                && printf '%s' "$response" | grep -Eq '"course"[[:space:]]*:[[:space:]]*[0-9]+' \
                && printf '%s' "$response" | grep -Eq '"lesson"[[:space:]]*:[[:space:]]*[0-9]+' \
                && printf '%s' "$response" | grep -Eq '"knowledge_tag"[[:space:]]*:[[:space:]]*[0-9]+' \
                && printf '%s' "$response" | grep -Eq '"hint"[[:space:]]*:[[:space:]]*[0-9]+' \
                && printf '%s' "$response" | grep -Eq '"event"[[:space:]]*:[[:space:]]*[0-9]+' \
                && printf '%s' "$response" | grep -Eq '"assignment"[[:space:]]*:[[:space:]]*[0-9]+' \
                || ctfctl_die "SMOKE_GRAPH_CONTRACT_FAILED" "knowledge-map V2 overview response is not the current contract"
        fi
        if [[ "$endpoint" == "/ctf/api/learn/knowledge-map/v2/canvas" ]]; then
            printf '%s' "$response" | grep -Eq '"graph_revision"[[:space:]]*:[[:space:]]*"[^"]+"' \
                && printf '%s' "$response" | grep -Eq '"nodes"[[:space:]]*:[[:space:]]*\[' \
                && printf '%s' "$response" | grep -Eq '"edges"[[:space:]]*:[[:space:]]*\[' \
                && printf '%s' "$response" | grep -Eq '"layout_hints"[[:space:]]*:[[:space:]]*\{' \
                && printf '%s' "$response" | grep -Eq '"applied_filters"[[:space:]]*:[[:space:]]*\{[^}]*"mode"[[:space:]]*:[[:space:]]*"canvas"' \
                && printf '%s' "$response" | grep -Eq '"next_cursor"[[:space:]]*:[[:space:]]*(null|"[^"]+")' \
                || ctfctl_die "SMOKE_GRAPH_CANVAS_CONTRACT_FAILED" "knowledge-map V2 canvas response is not the current contract"
        fi
        if [[ "$endpoint" == "/ctf/api/learn/knowledge-map/v2/search?*" ]]; then
            printf '%s' "$response" | grep -Eq '"query"[[:space:]]*:[[:space:]]*"[^"]*"' \
                && printf '%s' "$response" | grep -Eq '"items"[[:space:]]*:[[:space:]]*\[' \
                && printf '%s' "$response" | grep -Eq '"total"[[:space:]]*:[[:space:]]*[0-9]+' \
                && printf '%s' "$response" | grep -Eq '"limit"[[:space:]]*:[[:space:]]*[0-9]+' \
                || ctfctl_die "SMOKE_GRAPH_SEARCH_CONTRACT_FAILED" "knowledge-map V2 search response is not the current contract"
            local search_ref
            search_ref="$(printf '%s' "$response" | python3 -c '
import json, re, sys
data = json.load(sys.stdin)
items = data["items"]
if not isinstance(items, list):
    raise ValueError("invalid search items")
if items:
    item = items[0]
    kind, entity_id = item["type"], item["entity_id"]
    if kind not in {"challenge", "course", "lesson", "knowledge_tag", "hint", "event", "assignment"}:
        raise ValueError("invalid content type")
    if not isinstance(entity_id, str) or not re.fullmatch(r"[A-Za-z0-9-]{1,64}", entity_id):
        raise ValueError("invalid content identifier")
    print(kind + "\t" + entity_id)
')" || ctfctl_die "SMOKE_GRAPH_SEARCH_CONTRACT_FAILED" "public search content identifiers are invalid"
            if [[ -n "$search_ref" ]]; then
                IFS=$'\t' read -r search_type search_id <<< "$search_ref"
            fi
        fi
        ctfctl_ok "SMOKE_HTTP_OK" "public route responded successfully"
    done
    if [[ -n "$search_type" && -n "$search_id" ]]; then
        local focus_response
        focus_response="$(curl --fail --silent --show-error --location --max-time 15 --proto '=https' --tlsv1.2 \
            "$base_url/ctf/api/learn/knowledge-map/v2/focus?focus_type=$search_type&focus_id=$search_id&depth=1")" \
            || ctfctl_die "SMOKE_GRAPH_FOCUS_HTTP_FAILED" "knowledge-map V2 focus probe failed for a public search result"
        printf '%s' "$focus_response" | grep -Eq '"graph_revision"[[:space:]]*:[[:space:]]*"[^"]+"' \
            && printf '%s' "$focus_response" | grep -Eq '"focus"[[:space:]]*:[[:space:]]*\{' \
            && printf '%s' "$focus_response" | grep -Eq '"nodes"[[:space:]]*:[[:space:]]*\[' \
            && printf '%s' "$focus_response" | grep -Eq '"edges"[[:space:]]*:[[:space:]]*\[' \
            && printf '%s' "$focus_response" | grep -Eq '"layout_hints"[[:space:]]*:[[:space:]]*\{' \
            && printf '%s' "$focus_response" | grep -Eq '"applied_filters"[[:space:]]*:[[:space:]]*\{' \
            && printf '%s' "$focus_response" | grep -Eq '"next_cursor"[[:space:]]*:[[:space:]]*(null|"[^"]+")' \
            || ctfctl_die "SMOKE_GRAPH_FOCUS_CONTRACT_FAILED" "knowledge-map V2 focus response is not the current contract"
        ctfctl_ok "SMOKE_GRAPH_FOCUS_OK" "knowledge-map V2 focus contract passed"

        # Depth-two expansion must exercise both tag membership and taxonomy.
        # A deployment can serve a healthy full canvas while this combined
        # projection still fails at runtime, so keep it as a separate gate.
        local focus_expansion_response
        focus_expansion_response="$(curl --fail --silent --show-error --location --max-time 15 --proto '=https' --tlsv1.2 \
            "$base_url/ctf/api/learn/knowledge-map/v2/focus?focus_type=$search_type&focus_id=$search_id&depth=2&relation_types=tag_affinity&relation_types=tag_parent")" \
            || ctfctl_die "SMOKE_GRAPH_FOCUS_EXPANSION_HTTP_FAILED" "knowledge-map V2 depth-two tag expansion failed"
        printf '%s' "$focus_expansion_response" | grep -Eq '"graph_revision"[[:space:]]*:[[:space:]]*"[^"]+"' \
            && printf '%s' "$focus_expansion_response" | grep -Eq '"nodes"[[:space:]]*:[[:space:]]*\[' \
            && printf '%s' "$focus_expansion_response" | grep -Eq '"edges"[[:space:]]*:[[:space:]]*\[' \
            || ctfctl_die "SMOKE_GRAPH_FOCUS_EXPANSION_CONTRACT_FAILED" "knowledge-map V2 depth-two tag expansion response is invalid"
        ctfctl_ok "SMOKE_GRAPH_FOCUS_EXPANSION_OK" "knowledge-map V2 depth-two tag expansion passed"
    else
        ctfctl_ok "SMOKE_GRAPH_FOCUS_SKIPPED" "knowledge-map V2 focus needs a public content fixture; search returned no item"
    fi
    ctfctl_ok "SMOKE_PARTIAL" "unauthenticated HTTP probes passed only; email, login, SSO, uploads, flags, audit, generic Docker challenge fixtures, VMware, and production acceptance remain unverified"
}

ctfctl_require_root() {
    (( EUID == 0 )) || ctfctl_die "ROOT_REQUIRED" "this command must run as root because it writes managed /etc state or installs Docker Engine packages"
}

ctfctl_require_supported_ubuntu() {
    [[ -r /etc/os-release ]] || ctfctl_die "OS_RELEASE_MISSING" "cannot identify the Linux distribution"
    # shellcheck disable=SC1091
    . /etc/os-release
    [[ "${ID:-}" == "ubuntu" ]] || ctfctl_die "UNSUPPORTED_OS" "install supports clean Ubuntu hosts only"
    case "${VERSION_ID:-}" in
        22.04|24.04|26.04) ;;
        *) ctfctl_die "UNSUPPORTED_UBUNTU_VERSION" "install supports Ubuntu 22.04, 24.04, or 26.04 only" ;;
    esac
    [[ -n "${UBUNTU_CODENAME:-${VERSION_CODENAME:-}}" ]] || ctfctl_die "UBUNTU_CODENAME_MISSING" "Ubuntu release codename is unavailable"
}

ctfctl_assert_no_conflicting_container_packages() {
    local package_name
    local -a conflicts=(
        "docker.io"
        "docker-compose"
        "docker-compose-v2"
        "docker-doc"
        "docker-buildx"
        "podman-docker"
        "containerd"
        "runc"
    )

    for package_name in "${conflicts[@]}"; do
        if dpkg-query -W -f='${db:Status-Status}' "$package_name" 2>/dev/null | grep -Fxq "installed"; then
            ctfctl_die "CONFLICTING_CONTAINER_PACKAGE" "a conflicting container package is installed; do not remove it automatically on a host that may contain data"
        fi
    done
}

ctfctl_install_docker_engine() {
    local codename
    local source_file="/etc/apt/sources.list.d/docker.sources"
    local key_file="/etc/apt/keyrings/docker.asc"

    if command -v docker >/dev/null 2>&1 && docker compose version >/dev/null 2>&1; then
        ctfctl_ok "DOCKER_ALREADY_PRESENT" "Docker Engine and Compose are already available; package installation was skipped"
        return 0
    fi

    ctfctl_require_command "apt-get"
    ctfctl_require_command "curl"
    ctfctl_require_command "dpkg"
    ctfctl_require_command "dpkg-query"
    ctfctl_assert_no_conflicting_container_packages
    codename="${UBUNTU_CODENAME:-${VERSION_CODENAME:-}}"

    # Use Docker's signed apt repository, not a downloaded convenience script.
    # The caller has supplied the explicit install confirmation and this code is
    # only executed on an empty, supported Ubuntu host.
    DEBIAN_FRONTEND=noninteractive apt-get update -y
    DEBIAN_FRONTEND=noninteractive apt-get install -y ca-certificates curl
    install -m 0755 -d /etc/apt/keyrings
    if [[ ! -f "$key_file" ]]; then
        curl --fail --silent --show-error --location --proto '=https' --tlsv1.2 \
            https://download.docker.com/linux/ubuntu/gpg -o "$key_file"
        chmod a+r "$key_file"
    fi
    if [[ ! -f "$source_file" ]]; then
        printf '%s\n' \
            'Types: deb' \
            'URIs: https://download.docker.com/linux/ubuntu' \
            "Suites: ${codename}" \
            'Components: stable' \
            "Architectures: $(dpkg --print-architecture)" \
            "Signed-By: ${key_file}" > "$source_file"
    fi
    DEBIAN_FRONTEND=noninteractive apt-get update -y
    DEBIAN_FRONTEND=noninteractive apt-get install -y \
        docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
    systemctl enable --now docker
    docker version --format '{{.Server.Version}}' >/dev/null 2>&1 \
        || ctfctl_die "DOCKER_INSTALL_VERIFICATION_FAILED" "Docker Engine did not become reachable after package installation"
    docker compose version >/dev/null 2>&1 \
        || ctfctl_die "COMPOSE_INSTALL_VERIFICATION_FAILED" "Docker Compose plugin is unavailable after package installation"
    ctfctl_ok "DOCKER_INSTALL_COMPLETE" "Docker Engine and Compose were installed from Docker's signed Ubuntu apt repository"
}

ctfctl_install() {
    local requested_release_root="$1"
    local confirmation="$2"
    local install_id
    local scope_parent="/opt"
    local scope_root="/opt/ctf-web"
    local state_scope="/var/lib/ctf-web"

    ctfctl_require_linux
    ctfctl_require_root
    ctfctl_require_supported_ubuntu
    ctfctl_require_command "mkdir"
    ctfctl_require_command "realpath"
    ctfctl_require_command "stat"
    ctfctl_require_command "install"
    ctfctl_require_safe_absolute_path "$requested_release_root"
    install_id="$(basename -- "$requested_release_root")"
    ctfctl_require_safe_id "$install_id" "installation id"
    [[ "$confirmation" == "install:${install_id}" ]] || ctfctl_die "CONFIRMATION_MISMATCH" "install confirmation must be install:<release-root-basename>"
    ctfctl_is_descendant "$requested_release_root" "/opt/ctf-web" || ctfctl_die "INSTALL_ROOT_OUTSIDE_SCOPE" "release root must be below /opt/ctf-web"
    ctfctl_require_state_dir_scope
    [[ "$(dirname -- "$requested_release_root")" == "$scope_root" ]] || ctfctl_die "INSTALL_ROOT_NESTING_INVALID" "release root must be a direct child of /opt/ctf-web"
    [[ "$(dirname -- "$CTFCTL_STATE_DIR")" == "$state_scope" ]] || ctfctl_die "STATE_DIR_NESTING_INVALID" "state directory must be a direct child of /var/lib/ctf-web"
    ctfctl_install_docker_engine
    # install never configures firewall rules, domain/DNS, TLS, secrets, a
    # release payload, or application containers. Those remain explicit gates.
    ctfctl_create_private_dir_if_missing "$scope_parent"
    ctfctl_create_private_dir_if_missing "$scope_root"
    ctfctl_create_private_dir_if_missing "$requested_release_root"
    ctfctl_create_private_dir_if_missing "$state_scope"
    ctfctl_create_private_dir_if_missing "$CTFCTL_STATE_DIR"
    ctfctl_prepare_default_dynamic_flag_runtime_dir
    ctfctl_ensure_challenge_network
    ctfctl_ok "INSTALL_COMPLETE" "Docker prerequisites, private application directories, and the reviewed external challenge network are ready; secrets, firewall rules, releases, and containers were not changed"
}

ctfctl_init_jwt_secrets() {
    local confirmation="$1"
    local config_dir="/etc/ctf-web"
    local private_key
    local public_key
    local private_tmp
    local public_tmp

    ctfctl_require_linux
    ctfctl_require_root
    ctfctl_require_command "openssl"
    ctfctl_require_command "mktemp"
    ctfctl_require_command "mv"
    ctfctl_require_command "chmod"
    ctfctl_require_command "chown"
    [[ "$confirmation" == "init-jwt-secrets" ]] || ctfctl_die "CONFIRMATION_MISMATCH" "JWT initialization requires --confirm init-jwt-secrets"

    ctfctl_create_private_dir_if_missing "/etc"
    ctfctl_create_private_dir_if_missing "$config_dir"
    ctfctl_create_private_dir_if_missing "$CTFCTL_SECRETS_DIR"
    private_key="${CTFCTL_SECRETS_DIR}/${CTFCTL_JWT_PRIVATE_KEY_NAME}"
    public_key="${CTFCTL_SECRETS_DIR}/${CTFCTL_JWT_PUBLIC_KEY_NAME}"
    [[ ! -e "$private_key" && ! -L "$private_key" && ! -e "$public_key" && ! -L "$public_key" ]] \
        || ctfctl_die "JWT_KEY_ALREADY_EXISTS" "refusing to overwrite an existing JWT key; rotation is a separate approved procedure"

    private_tmp="$(mktemp "${CTFCTL_SECRETS_DIR}/.jwt-private.XXXXXX")"
    public_tmp="$(mktemp "${CTFCTL_SECRETS_DIR}/.jwt-public.XXXXXX")"
    trap 'rm -f -- "$private_tmp" "$public_tmp"' EXIT

    openssl genpkey -algorithm RSA -out "$private_tmp" -pkeyopt rsa_keygen_bits:3072 >/dev/null 2>&1 \
        || ctfctl_die "JWT_PRIVATE_KEY_GENERATION_FAILED" "JWT private key generation failed"
    openssl pkey -in "$private_tmp" -pubout -out "$public_tmp" >/dev/null 2>&1 \
        || ctfctl_die "JWT_PUBLIC_KEY_GENERATION_FAILED" "JWT public key generation failed"
    chown root:root "$private_tmp" "$public_tmp"
    chmod 0600 "$private_tmp"
    chmod 0644 "$public_tmp"
    mv -f -- "$private_tmp" "$private_key"
    mv -f -- "$public_tmp" "$public_key"
    trap - EXIT

    ctfctl_ok "JWT_KEY_INITIALIZED" "new JWT key files were created outside the release tree; key contents and paths were not printed"
}
