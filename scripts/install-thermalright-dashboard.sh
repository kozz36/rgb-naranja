#!/usr/bin/env bash
# Install, remove, or roll back only the per-user Thermalright dashboard bundle.
# USB access remains deliberately separate: this script never uses sudo, pkexec,
# udev, linger, OpenRGB, Naga, or dependency installation.
set -euo pipefail
umask 077

usage() {
    cat <<'EOF'
Usage: install-thermalright-dashboard.sh [--start] [--jpeg-encoder SOURCE_EXECUTABLE]
       install-thermalright-dashboard.sh --uninstall
       install-thermalright-dashboard.sh --rollback BACKUP_DIR

Default: stage and atomically install/update the managed five-module dashboard
bundle in ~/.local/bin and its user unit in ~/.config/systemd/user, then run
systemctl --user daemon-reload. It never stops, enables, starts, or restarts an
active unit. Every successful install creates a new private backup and prints
its exact path as BACKUP_DIR=/absolute/path.

--start: preflight and stage the whole bundle, stop an originally active unit
before publishing any module, then publish the complete bundle, daemon-reload,
enable, start, and perform a short process-stability check. A failure after
publication restores the backed-up files and original activation state.

--jpeg-encoder SOURCE_EXECUTABLE: opt in to copying one explicitly supplied,
trusted ELF executable into the private managed encoder directory. Its bytes are
read and checked but never run by this installer. The staged unit receives only
the fixed %h-managed encoder path; the supplied source path is never interpolated
into unit text. Without this option the unit remains GPU+Qt, while an existing
valid managed encoder pair is preserved.

--uninstall: disable/stop the unit only when its exact managed copy exists,
then remove only exact managed copies of all five modules, the unit, and a valid
managed encoder pair. It never removes dashboard backups or unrelated files.

--rollback BACKUP_DIR: restore a backup created by this installer. BACKUP_DIR
must be a private, direct child of
~/.local/share/rgb-naranja/dashboard-backups with an intact fixed manifest and
backup files; symlinks, escaped paths, unowned current targets, and malformed
backups are refused before stopping the service. Rollback stops the current
service, restores the complete bundle, daemon-reloads, then restores the saved
enabled and active state.

The required venv is ~/.local/share/rgb-naranja/trcc-venv/bin/python. Its
availability check uses importlib.util.find_spec for trcc, PySide6, and numpy;
it does not import or boot TRCC, touch USB, install dependencies, or modify the
host beyond the requested user files and user-manager calls. The 3-second
--start check confirms only systemd activity and a stable MainPID; it does not
prove USB or panel health. Inspect logs and perform separate host validation.
EOF
}

die() {
    printf '%s\n' "$*" >&2
    exit 1
}

MODE=install
START=false
ROLLBACK_DIR=""
JPEG_ENCODER_SOURCE=""
JPEG_ENCODER=false
if [ "$#" -eq 1 ] && { [ "$1" = --help ] || [ "$1" = -h ]; }; then
    usage
    exit 0
fi
if [ "$#" -eq 1 ] && [ "$1" = --uninstall ]; then
    MODE=uninstall
elif [ "$#" -eq 2 ] && [ "$1" = --rollback ]; then
    MODE=rollback
    ROLLBACK_DIR="$2"
else
    while [ "$#" -gt 0 ]; do
        case "$1" in
            --start)
                [ "$START" = false ] || { usage >&2; exit 2; }
                START=true
                shift
                ;;
            --jpeg-encoder)
                [ "$JPEG_ENCODER" = false ] && [ "$#" -ge 2 ] || { usage >&2; exit 2; }
                JPEG_ENCODER=true
                JPEG_ENCODER_SOURCE="$2"
                shift 2
                ;;
            *) usage >&2; exit 2 ;;
        esac
    done
fi

[ "${EUID}" -ne 0 ] || die "Refusing root: this installer owns only user files."
case "${HOME:-}" in
    /*) ;;
    *) die "HOME must be an absolute path." ;;
esac
[ -d "$HOME" ] && [ ! -L "$HOME" ] || die "HOME must be a non-symlink directory: $HOME"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
CONFIG_HOME="${XDG_CONFIG_HOME:-$HOME/.config}"
case "$CONFIG_HOME" in
    /*) ;;
    *) die "XDG_CONFIG_HOME must be an absolute path: $CONFIG_HOME" ;;
esac

USER_BIN_DIR="$HOME/.local/bin"
USER_UNIT_DIR="$CONFIG_HOME/systemd/user"
BACKUP_ROOT="$HOME/.local/share/rgb-naranja/dashboard-backups"
ENCODER_DIR="$HOME/.local/share/rgb-naranja/thermalright-dashboard-jpeg"
ENCODER_NAME="rust-dashboard-jpeg"
ENCODER_MANIFEST_NAME="rust-dashboard-jpeg.manifest"
UNIT_NAME="thermalright-dashboard.service"
MANAGED_MARKER="# THERMALRIGHT_DASHBOARD_MANAGED_COPY"
ENCODER_MANIFEST_VERSION="thermalright-dashboard-jpeg-v1"
VENV_PYTHON="$HOME/.local/share/rgb-naranja/trcc-venv/bin/python"
EXPECTED_QT_EXEC_START="ExecStart=%h/.local/share/rgb-naranja/trcc-venv/bin/python %h/.local/bin/thermalright-dashboard.py --continuous --renderer cinematic-gpu"
RUST_EXEC_START="$EXPECTED_QT_EXEC_START --jpeg-encoder %h/.local/share/rgb-naranja/thermalright-dashboard-jpeg/rust-dashboard-jpeg"

TARGET_LABELS=(
    "thermalright-dashboard.py"
    "thermalright_cinematic.py"
    "thermalright_cinematic_runtime.py"
    "thermalright_cinematic_gpu.py"
    "thermalright_jpeg.py"
    "thermalright-dashboard.service"
    "$ENCODER_NAME"
    "$ENCODER_MANIFEST_NAME"
)
TARGET_PATHS=(
    "$USER_BIN_DIR/thermalright-dashboard.py"
    "$USER_BIN_DIR/thermalright_cinematic.py"
    "$USER_BIN_DIR/thermalright_cinematic_runtime.py"
    "$USER_BIN_DIR/thermalright_cinematic_gpu.py"
    "$USER_BIN_DIR/thermalright_jpeg.py"
    "$USER_UNIT_DIR/$UNIT_NAME"
    "$ENCODER_DIR/$ENCODER_NAME"
    "$ENCODER_DIR/$ENCODER_MANIFEST_NAME"
)
SOURCE_PATHS=(
    "$SCRIPT_DIR/thermalright-dashboard.py"
    "$SCRIPT_DIR/thermalright_cinematic.py"
    "$SCRIPT_DIR/thermalright_cinematic_runtime.py"
    "$SCRIPT_DIR/thermalright_cinematic_gpu.py"
    "$SCRIPT_DIR/thermalright_jpeg.py"
    "$SCRIPT_DIR/../systemd/$UNIT_NAME"
)
TARGET_MODES=(0755 0644 0644 0644 0644 0644 0755 0600)
MODULE_TARGET_INDICES=(0 1 2 3 4)
UNIT_TARGET_INDEX=5
BINARY_TARGET_INDEX=6
METADATA_TARGET_INDEX=7
INSTALL_STAGE_INDICES=(0 1 2 3 4 5)
V1_TARGET_INDICES=(0 1 2 "$UNIT_TARGET_INDEX")
V2_TARGET_INDICES=(0 1 2 3 "$UNIT_TARGET_INDEX")
STAGED=("" "" "" "" "" "" "" "")
TRANSFORM_STAGED=""
STAGED_HASHES=("" "" "" "" "" "" "" "")
BACKUP_PRESENT=("" "" "" "" "" "" "" "")
BACKUP_MODES=("" "" "" "" "" "" "" "")
BACKUP_HASHES=("" "" "" "" "" "" "" "")
BACKUP_MANIFEST_INDICES=()
BACKUP_VERSION=""
BACKUP_ACTIVE=""
BACKUP_ENABLED=""
BACKUP_DIR=""
CURRENT_EUID="$EUID"
PREVALIDATED_HASHES=("" "" "" "" "" "" "" "")
PUBLISHED=(false false false false false false false false)
TRANSACTION_PHASE=unarmed
TRANSACTION_RECOVERY_ENTRY_PHASE=""
TRANSACTION_ARMED=false
TRANSACTION_RECOVERY_ATTEMPTED=false
TRANSACTION_RECOVERY_IN_PROGRESS=false
TRANSACTION_STOP_CURRENT=false
TRANSACTION_RESTORE_ACTIVATION=false
RECOVERY_SYSTEMCTL_TIMEOUT_S=15

ensure_child_directory() {
    local directory="$1" mode="$2" parent
    [ ! -L "$directory" ] || die "Refusing unexpected symlinked directory: $directory"
    if [ -e "$directory" ]; then
        [ -d "$directory" ] || die "Expected directory, found another file: $directory"
        return
    fi
    parent="$(dirname "$directory")"
    [ -d "$parent" ] && [ ! -L "$parent" ] || die "Expected non-symlink parent directory: $parent"
    mkdir -m "$mode" -- "$directory"
}

ensure_target_directories() {
    ensure_child_directory "$HOME/.local" 0700
    ensure_child_directory "$USER_BIN_DIR" 0700
    ensure_child_directory "$CONFIG_HOME" 0700
    ensure_child_directory "$CONFIG_HOME/systemd" 0700
    ensure_child_directory "$USER_UNIT_DIR" 0700
}

ensure_encoder_directory() {
    ensure_child_directory "$HOME/.local" 0700
    ensure_child_directory "$HOME/.local/share" 0700
    ensure_child_directory "$HOME/.local/share/rgb-naranja" 0700
    ensure_child_directory "$ENCODER_DIR" 0700
    is_private_encoder_directory || die "Refusing unsafe managed encoder directory: $ENCODER_DIR"
}

ensure_backup_root() {
    ensure_child_directory "$HOME/.local" 0700
    ensure_child_directory "$HOME/.local/share" 0700
    ensure_child_directory "$HOME/.local/share/rgb-naranja" 0700
    ensure_child_directory "$BACKUP_ROOT" 0700
    chmod 0700 -- "$BACKUP_ROOT"
}

path_has_no_symlink_component() {
    local path="$1" component segment
    local -a path_components
    [ "${path#/}" != "$path" ] || return 1
    component=""
    IFS=/ read -r -a path_components <<< "${path#/}"
    for segment in "${path_components[@]}"; do
        [ -n "$segment" ] || continue
        component="$component/$segment"
        [ ! -L "$component" ] || return 1
    done
}

validate_user_storage_root() {
    local root="$1" label="$2" component="" segment owner mode numeric sticky_ancestor=""
    local -a root_components
    case "$root" in
        /*) ;;
        *) die "$label must be an absolute path: $root" ;;
    esac
    IFS=/ read -r -a root_components <<< "${root#/}"
    for segment in "${root_components[@]}"; do
        [ -n "$segment" ] || continue
        case "$segment" in
            .|..) die "$label must not contain lexical '.' or '..' components: $root" ;;
        esac
        component="$component/$segment"
        [ ! -L "$component" ] || die "Refusing $label with a symlinked ancestor: $component"
        if [ -e "$component" ]; then
            [ -d "$component" ] || die "Expected directory in $label ancestry: $component"
            owner="$(stat -c '%u' -- "$component")" \
                || die "Unable to inspect $label ancestor: $component"
            mode="$(stat -c '%a' -- "$component")" \
                || die "Unable to inspect $label ancestor mode: $component"
            [[ "$mode" =~ ^[0-7]{3,4}$ ]] \
                || die "Unexpected $label ancestor mode: $component"
            numeric=$((8#$mode))
            if { [ "$owner" = "$CURRENT_EUID" ] || [ "$owner" = 0 ]; } \
                && [ $((numeric & 0022)) -eq 0 ]; then
                sticky_ancestor=""
                continue
            fi
            if [ "$owner" = 0 ] && [ $((numeric & 01000)) -ne 0 ]; then
                sticky_ancestor="$component"
                continue
            fi
            die "Refusing unsafe $label ancestor: $component"
        fi
    done
    [ -z "$sticky_ancestor" ] \
        || die "Refusing $label beneath an untrusted root-owned sticky directory: $sticky_ancestor"
}

validate_venv_interpreter_directory() {
    local directory="$1" label="$2"
    [ -d "$directory" ] && [ ! -L "$directory" ] \
        || die "Refusing $label with a missing or symlinked directory: $directory"
    validate_user_storage_root "$directory" "$label"
}

validate_venv_interpreter_directory_component() {
    local directory="$1" label="$2" owner mode numeric
    [ -d "$directory" ] && [ ! -L "$directory" ] \
        || die "Refusing $label with a missing or symlinked directory: $directory"
    owner="$(stat -c '%u' -- "$directory")" \
        || die "Unable to inspect $label ownership: $directory"
    mode="$(stat -c '%a' -- "$directory")" \
        || die "Unable to inspect $label mode: $directory"
    [[ "$mode" =~ ^[0-7]{3,4}$ ]] \
        || die "Unexpected $label mode: $directory"
    numeric=$((8#$mode))
    if { [ "$owner" = "$CURRENT_EUID" ] || [ "$owner" = 0 ]; } \
        && [ $((numeric & 0022)) -eq 0 ]; then
        return
    fi
    # This provisional sticky-root allowance is resolved by the full parent
    # validation before any leaf can be followed or executed.
    [ "$owner" = 0 ] && [ $((numeric & 01000)) -ne 0 ] \
        || die "Refusing unsafe $label directory: $directory"
}

resolve_venv_interpreter_link_target() {
    local link_parent="$1" link_target="$2" candidate segment index
    local -a link_components
    [ -n "$link_target" ] || die "Refusing venv interpreter with an empty symlink target."
    case "$link_target" in
        /) VENV_INTERPRETER_RESOLVED_PATH=/; return ;;
        */) die "Refusing venv interpreter with a directory symlink target: $link_target" ;;
    esac
    case "$link_target" in
        /*)
            candidate=/
            link_target="${link_target#/}"
            ;;
        *) candidate="$link_parent" ;;
    esac
    IFS=/ read -r -a link_components <<< "$link_target"
    for index in "${!link_components[@]}"; do
        segment="${link_components[$index]}"
        [ -n "$segment" ] || continue
        case "$segment" in
            .) continue ;;
            ..)
                candidate="${candidate%/*}"
                [ -n "$candidate" ] || candidate=/
                continue
                ;;
        esac
        if [ "$candidate" = / ]; then
            candidate="/$segment"
        else
            candidate="$candidate/$segment"
        fi
        if [ "$index" -lt "$(( ${#link_components[@]} - 1 ))" ]; then
            validate_venv_interpreter_directory_component "$candidate" "venv interpreter link target"
        fi
    done
    VENV_INTERPRETER_RESOLVED_PATH="$candidate"
}

validate_venv_interpreter() {
    local candidate="$1" link_parent link_target owner mode numeric hops=0
    case "$candidate" in
        /*) ;;
        *) die "Venv interpreter must be an absolute path: $candidate" ;;
    esac
    while :; do
        link_parent="${candidate%/*}"
        [ -n "$link_parent" ] || link_parent=/
        validate_venv_interpreter_directory "$link_parent" "venv interpreter parent"
        if [ -L "$candidate" ]; then
            hops=$((hops + 1))
            [ "$hops" -le 40 ] \
                || die "Refusing venv interpreter with too many symlink hops: $VENV_PYTHON"
            link_target="$(readlink -- "$candidate")" \
                || die "Refusing venv interpreter with an unreadable symlink: $candidate"
            resolve_venv_interpreter_link_target "$link_parent" "$link_target"
            candidate="$VENV_INTERPRETER_RESOLVED_PATH"
            continue
        fi
        [ -e "$candidate" ] || die "Refusing missing or dangling venv interpreter: $candidate"
        [ -f "$candidate" ] && [ -x "$candidate" ] \
            || die "Refusing venv interpreter that is not a regular executable: $candidate"
        owner="$(stat -c '%u' -- "$candidate")" \
            || die "Unable to inspect venv interpreter ownership: $candidate"
        mode="$(stat -c '%a' -- "$candidate")" \
            || die "Unable to inspect venv interpreter mode: $candidate"
        [[ "$mode" =~ ^[0-7]{3,4}$ ]] \
            || die "Unexpected venv interpreter mode: $candidate"
        numeric=$((8#$mode))
        { [ "$owner" = "$CURRENT_EUID" ] || [ "$owner" = 0 ]; } \
            && [ $((numeric & 0022)) -eq 0 ] \
            || die "Refusing unsafe venv interpreter: $candidate"
        return
    done
}

preflight_user_storage_roots() {
    validate_user_storage_root "$HOME" "HOME"
    validate_user_storage_root "$USER_BIN_DIR" "user bin root"
    validate_user_storage_root "$CONFIG_HOME" "XDG_CONFIG_HOME"
    validate_user_storage_root "$USER_UNIT_DIR" "user unit root"
    validate_user_storage_root "$BACKUP_ROOT" "backup root"
    validate_user_storage_root "$ENCODER_DIR" "JPEG encoder root"
    validate_user_storage_root "$(dirname "$VENV_PYTHON")" "dashboard venv root"
    validate_venv_interpreter "$VENV_PYTHON"
}

is_managed_copy() {
    [ -f "$1" ] && [ ! -L "$1" ] && grep -Fqx "$MANAGED_MARKER" "$1"
}

refuse_unowned_target() {
    local target="$1"
    path_has_no_symlink_component "$target" || die "Refusing target path with a symlinked component: $target"
    [ ! -L "$target" ] || die "Refusing unexpected symlink target: $target"
    if [ -e "$target" ]; then
        is_managed_copy "$target" || die "Refusing to overwrite or remove unowned target: $target"
        is_owned_by_current_euid "$target" || die "Refusing target not owned by the current effective user: $target"
    fi
}

preflight_targets() {
    local index
    for index in "${INSTALL_STAGE_INDICES[@]}"; do
        refuse_unowned_target "${TARGET_PATHS[$index]}"
    done
    validate_current_encoder_pair || die "Refusing unsafe managed encoder pair."
}

preflight_sources_and_venv() {
    local index source
    for index in "${!SOURCE_PATHS[@]}"; do
        source="${SOURCE_PATHS[$index]}"
        [ -f "$source" ] && [ ! -L "$source" ] && [ -r "$source" ] \
            || die "Missing, unreadable, or symlinked source: $source"
    done
    if [ "$JPEG_ENCODER" = true ]; then
        validate_encoder_source "$JPEG_ENCODER_SOURCE"
    fi
    [ -x "$VENV_PYTHON" ] || die "Missing venv interpreter: $VENV_PYTHON"
    "$VENV_PYTHON" -c 'import importlib.util, sys; sys.exit(0 if all(importlib.util.find_spec(name) is not None for name in ("trcc", "PySide6", "numpy")) else 1)' \
        || die "The dashboard venv must provide trcc, PySide6, and numpy."
}

cleanup_staged() {
    local staged
    for staged in "${STAGED[@]}"; do
        if [ -n "$staged" ] && [ -e "$staged" ]; then
            rm -f -- "$staged"
        fi
    done
    if [ -n "$TRANSFORM_STAGED" ] && { [ -e "$TRANSFORM_STAGED" ] || [ -L "$TRANSFORM_STAGED" ]; }; then
        rm -f -- "$TRANSFORM_STAGED"
    fi
}

stage_managed_copy() {
    local source="$1" staged="$2" mode="$3" first_line
    if grep -Fqx "$MANAGED_MARKER" "$source"; then
        cat -- "$source" > "$staged"
    elif IFS= read -r first_line < "$source" && [[ "$first_line" == '#!'* ]]; then
        {
            printf '%s\n' "$first_line"
            printf '%s\n' "$MANAGED_MARKER"
            tail -n +2 -- "$source"
        } > "$staged"
    else
        {
            printf '%s\n' "$MANAGED_MARKER"
            cat -- "$source"
        } > "$staged"
    fi
    chmod "$mode" -- "$staged"
}

transform_staged_unit_for_encoder() {
    local staged="$1" transformed line expected_count exec_count
    expected_count="$(grep -Fxc -- "$EXPECTED_QT_EXEC_START" "$staged" || true)"
    exec_count="$(grep -c '^ExecStart=' -- "$staged" || true)"
    [ "$expected_count" = 1 ] && [ "$exec_count" = 1 ] \
        || die "Source unit must contain exactly one expected continuous cinematic-gpu ExecStart."
    transformed="$(mktemp "$(dirname "$staged")/.${UNIT_NAME}.transform.XXXXXX")" \
        || die "Unable to stage transformed user unit."
    TRANSFORM_STAGED="$transformed"
    while IFS= read -r line || [ -n "$line" ]; do
        if [ "$line" = "$EXPECTED_QT_EXEC_START" ]; then
            printf '%s\n' "$RUST_EXEC_START"
        else
            printf '%s\n' "$line"
        fi
    done < "$staged" > "$transformed"
    chmod "${TARGET_MODES[$UNIT_TARGET_INDEX]}" -- "$transformed"
    mv -f -- "$transformed" "$staged"
    TRANSFORM_STAGED=""
}

stage_bundle() {
    local index target staged
    for index in "${INSTALL_STAGE_INDICES[@]}"; do
        target="${TARGET_PATHS[$index]}"
        staged="$(mktemp "$(dirname "$target")/.${TARGET_LABELS[$index]}.new.XXXXXX")" \
            || die "Unable to stage managed copy beside target: $target"
        STAGED[index]="$staged"
        stage_managed_copy "${SOURCE_PATHS[$index]}" "$staged" "${TARGET_MODES[$index]}" \
            || die "Unable to stage managed copy: ${SOURCE_PATHS[$index]}"
        if [ "$index" -eq "$UNIT_TARGET_INDEX" ] && [ "$JPEG_ENCODER" = true ]; then
            transform_staged_unit_for_encoder "$staged"
        fi
        STAGED_HASHES[index]="$(checksum "$staged")"
    done
}

stage_encoder_pair() {
    local binary_staged metadata_staged binary_hash
    [ "$JPEG_ENCODER" = true ] || return 0
    binary_staged="$(mktemp "$ENCODER_DIR/.${ENCODER_NAME}.new.XXXXXX")" \
        || die "Unable to stage supplied JPEG encoder."
    STAGED[BINARY_TARGET_INDEX]="$binary_staged"
    cp -- "$JPEG_ENCODER_SOURCE" "$binary_staged" \
        || die "Unable to read supplied JPEG encoder."
    chmod 0755 -- "$binary_staged"
    binary_hash="$(checksum "$binary_staged")"
    STAGED_HASHES[BINARY_TARGET_INDEX]="$binary_hash"
    metadata_staged="$(mktemp "$ENCODER_DIR/.${ENCODER_MANIFEST_NAME}.new.XXXXXX")" \
        || die "Unable to stage JPEG encoder metadata."
    STAGED[METADATA_TARGET_INDEX]="$metadata_staged"
    printf '%s\nsha256=%s\nmode=0755\n' "$ENCODER_MANIFEST_VERSION" "$binary_hash" > "$metadata_staged"
    chmod 0600 -- "$metadata_staged"
    STAGED_HASHES[METADATA_TARGET_INDEX]="$(checksum "$metadata_staged")"
}

checksum() {
    sha256sum -- "$1" | awk '{print $1}'
}

is_owned_by_current_euid() {
    [ "$(stat -c '%u' -- "$1")" = "$CURRENT_EUID" ]
}

mode_is_safe_encoder_source() {
    local mode="$1" numeric
    [[ "$mode" =~ ^[0-7]{3,4}$ ]] || return 1
    numeric=$((8#$mode))
    [ $((numeric & 07022)) -eq 0 ]
}

validate_encoder_source() {
    local source="$1" parent mode owner header private_parent=false segment
    case "$source" in
        /*) ;;
        *) die "--jpeg-encoder must be an absolute path." ;;
    esac
    IFS=/ read -r -a _encoder_source_parts <<< "${source#/}"
    for segment in "${_encoder_source_parts[@]}"; do
        [ "$segment" != .. ] || die "--jpeg-encoder must not contain '..'."
    done
    path_has_no_symlink_component "$source" || die "--jpeg-encoder path has a symlinked component."
    [ -f "$source" ] && [ ! -L "$source" ] && [ -r "$source" ] && [ -x "$source" ] \
        || die "--jpeg-encoder must be a readable regular executable, not a symlink."
    is_owned_by_current_euid "$source" || die "--jpeg-encoder must be owned by the current effective user."
    mode="$(stat -c '%a' -- "$source")"
    mode_is_safe_encoder_source "$mode" \
        || die "--jpeg-encoder must not have group/other write or special permission bits."
    parent="$(dirname "$source")"
    while [ "$parent" != / ]; do
        [ -d "$parent" ] && [ ! -L "$parent" ] || die "--jpeg-encoder has an unsafe parent: $parent"
        owner="$(stat -c '%u' -- "$parent")"
        mode="$(stat -c '%a' -- "$parent")"
        { [ "$owner" = "$CURRENT_EUID" ] || [ "$owner" = 0 ]; } \
            && [ $((8#$mode & 0022)) -eq 0 ] \
            || die "--jpeg-encoder has an unsafe parent: $parent"
        if [ "$owner" = "$CURRENT_EUID" ] && [ $((8#$mode & 0077)) -eq 0 ]; then
            private_parent=true
            break
        fi
        parent="$(dirname "$parent")"
    done
    [ "$private_parent" = true ] \
        || die "--jpeg-encoder must be beneath a private effective-user directory."
    header="$(LC_ALL=C od -An -v -tx1 -N4 -- "$source" | tr -d '[:space:]')" \
        || die "Unable to read supplied JPEG encoder header."
    [ "$header" = 7f454c46 ] || die "--jpeg-encoder must have an ELF header."
}

is_private_encoder_directory() {
    [ -d "$ENCODER_DIR" ] && [ ! -L "$ENCODER_DIR" ] \
        && path_has_no_symlink_component "$ENCODER_DIR" \
        && [ "$(stat -c '%a' -- "$ENCODER_DIR")" = 700 ] \
        && is_owned_by_current_euid "$ENCODER_DIR"
}

read_encoder_metadata() {
    local metadata="$1"
    local -a lines
    mapfile -t lines < "$metadata" || return 1
    [ "${#lines[@]}" -eq 3 ] || return 1
    [ "${lines[0]}" = "$ENCODER_MANIFEST_VERSION" ] || return 1
    [[ "${lines[1]}" =~ ^sha256=[0-9a-f]{64}$ ]] || return 1
    [ "${lines[2]}" = mode=0755 ] || return 1
    ENCODER_METADATA_HASH="${lines[1]#sha256=}"
}

validate_current_encoder_pair() {
    local binary_present=false metadata_present=false
    { [ -e "${TARGET_PATHS[$BINARY_TARGET_INDEX]}" ] || [ -L "${TARGET_PATHS[$BINARY_TARGET_INDEX]}" ]; } \
        && binary_present=true
    { [ -e "${TARGET_PATHS[$METADATA_TARGET_INDEX]}" ] || [ -L "${TARGET_PATHS[$METADATA_TARGET_INDEX]}" ]; } \
        && metadata_present=true
    if [ "$binary_present" = false ] && [ "$metadata_present" = false ]; then
        return 0
    fi
    [ "$binary_present" = true ] && [ "$metadata_present" = true ] || return 1
    is_private_encoder_directory || return 1
    [ -f "${TARGET_PATHS[$BINARY_TARGET_INDEX]}" ] && [ ! -L "${TARGET_PATHS[$BINARY_TARGET_INDEX]}" ] \
        && is_owned_by_current_euid "${TARGET_PATHS[$BINARY_TARGET_INDEX]}" \
        && [ "$(stat -c '%a' -- "${TARGET_PATHS[$BINARY_TARGET_INDEX]}")" = 755 ] \
        || return 1
    [ -f "${TARGET_PATHS[$METADATA_TARGET_INDEX]}" ] && [ ! -L "${TARGET_PATHS[$METADATA_TARGET_INDEX]}" ] \
        && is_owned_by_current_euid "${TARGET_PATHS[$METADATA_TARGET_INDEX]}" \
        && [ "$(stat -c '%a' -- "${TARGET_PATHS[$METADATA_TARGET_INDEX]}")" = 600 ] \
        || return 1
    read_encoder_metadata "${TARGET_PATHS[$METADATA_TARGET_INDEX]}" \
        && [ "$(checksum "${TARGET_PATHS[$BINARY_TARGET_INDEX]}")" = "$ENCODER_METADATA_HASH" ]
}

capture_service_state() {
    BACKUP_ACTIVE="$(systemctl --user is-active "$UNIT_NAME" 2>/dev/null || true)"
    BACKUP_ENABLED="$(systemctl --user is-enabled "$UNIT_NAME" 2>/dev/null || true)"
    case "$BACKUP_ACTIVE" in
        active|inactive|not-found) ;;
        *) die "Refusing ambiguous user-unit active state: ${BACKUP_ACTIVE:-<empty>}" ;;
    esac
    case "$BACKUP_ENABLED" in
        enabled|disabled|not-found) ;;
        *) die "Refusing ambiguous or masked user-unit enabled state: ${BACKUP_ENABLED:-<empty>}" ;;
    esac
}

create_backup() {
    local index target backup_file mode hash timestamp
    ensure_backup_root
    timestamp="$(date -u +%Y%m%dT%H%M%SZ)"
    BACKUP_DIR="$(mktemp -d "$BACKUP_ROOT/dashboard-backup-$timestamp-XXXXXX")" \
        || die "Unable to create private dashboard backup directory."
    chmod 0700 -- "$BACKUP_DIR"
    : > "$BACKUP_DIR/manifest"
    chmod 0600 -- "$BACKUP_DIR/manifest"
    {
        printf '%s\n' "thermalright-dashboard-backup-v3"
        printf 'active_state=%s\n' "$BACKUP_ACTIVE"
        printf 'enabled_state=%s\n' "$BACKUP_ENABLED"
        for index in "${!TARGET_PATHS[@]}"; do
            target="${TARGET_PATHS[$index]}"
            backup_file="$BACKUP_DIR/${TARGET_LABELS[$index]}"
            if [ -e "$target" ]; then
                cp -- "$target" "$backup_file"
                chmod 0600 -- "$backup_file"
                mode="$(stat -c '%a' -- "$target")"
                hash="$(checksum "$backup_file")"
                printf '%s|present|%s|%s\n' "${TARGET_LABELS[$index]}" "$mode" "$hash"
            else
                printf '%s|absent|-|-\n' "${TARGET_LABELS[$index]}"
            fi
        done
    } > "$BACKUP_DIR/manifest"
    chmod 0600 -- "$BACKUP_DIR/manifest"
}

backup_error() {
    printf 'Invalid dashboard backup: %s\n' "$*" >&2
    return 1
}

read_backup_manifest() {
    local manifest="$1" line label presence mode hash extra index
    BACKUP_PRESENT=("" "" "" "" "" "" "" "")
    BACKUP_MODES=("" "" "" "" "" "" "" "")
    BACKUP_HASHES=("" "" "" "" "" "" "" "")
    BACKUP_MANIFEST_INDICES=()
    BACKUP_VERSION=""
    exec 3< "$manifest" || return 1
    IFS= read -r line <&3 || return 1
    case "$line" in
        thermalright-dashboard-backup-v1)
            BACKUP_VERSION=v1
            BACKUP_MANIFEST_INDICES=("${V1_TARGET_INDICES[@]}")
            ;;
        thermalright-dashboard-backup-v2)
            BACKUP_VERSION=v2
            BACKUP_MANIFEST_INDICES=("${V2_TARGET_INDICES[@]}")
            ;;
        thermalright-dashboard-backup-v3)
            BACKUP_VERSION=v3
            BACKUP_MANIFEST_INDICES=("${!TARGET_LABELS[@]}")
            ;;
        *) return 1 ;;
    esac
    IFS= read -r line <&3 \
            && { [ "$line" = "active_state=active" ] \
                || [ "$line" = "active_state=inactive" ] \
                || [ "$line" = "active_state=not-found" ]; } \
            || return 1
    BACKUP_ACTIVE="${line#active_state=}"
    IFS= read -r line <&3 \
            && { [ "$line" = "enabled_state=enabled" ] \
                || [ "$line" = "enabled_state=disabled" ] \
                || [ "$line" = "enabled_state=not-found" ]; } \
            || return 1
    BACKUP_ENABLED="${line#enabled_state=}"
    for index in "${BACKUP_MANIFEST_INDICES[@]}"; do
        IFS= read -r line <&3 || return 1
        IFS='|' read -r label presence mode hash extra <<< "$line"
        [ "$label" = "${TARGET_LABELS[$index]}" ] && [ -z "$extra" ] || return 1
        case "$presence" in
            present)
                [[ "$mode" =~ ^[0-7]{3,4}$ ]] && [[ "$hash" =~ ^[0-9a-f]{64}$ ]] || return 1
                ;;
            absent)
                [ "$mode" = "-" ] && [ "$hash" = "-" ] || return 1
                ;;
            *) return 1 ;;
        esac
        BACKUP_PRESENT[index]="$presence"
        BACKUP_MODES[index]="$mode"
        BACKUP_HASHES[index]="$hash"
    done
    if IFS= read -r line <&3; then
        return 1
    fi
    exec 3<&-
}

backup_presence_for_index() {
    local index="$1" listed_index
    for listed_index in "${BACKUP_MANIFEST_INDICES[@]}"; do
        if [ "$listed_index" -eq "$index" ]; then
            printf '%s' "${BACKUP_PRESENT[$index]}"
            return
        fi
    done
    printf '%s' absent
}

backup_file_is_expected() {
    local name="$1" index
    [ "$name" = manifest ] && return 0
    for index in "${BACKUP_MANIFEST_INDICES[@]}"; do
        [ "${BACKUP_PRESENT[$index]}" = present ] && [ "$name" = "${TARGET_LABELS[$index]}" ] && return 0
    done
    return 1
}

validate_backup_encoder_pair() {
    local binary_presence metadata_presence binary_file metadata_file
    binary_presence="$(backup_presence_for_index "$BINARY_TARGET_INDEX")"
    metadata_presence="$(backup_presence_for_index "$METADATA_TARGET_INDEX")"
    [ "$binary_presence" = "$metadata_presence" ] || return 1
    [ "$binary_presence" = absent ] && return 0
    [ "$BACKUP_VERSION" = v3 ] \
        && [ "${BACKUP_MODES[$BINARY_TARGET_INDEX]}" = 755 ] \
        && [ "${BACKUP_MODES[$METADATA_TARGET_INDEX]}" = 600 ] \
        || return 1
    binary_file="$BACKUP_DIR/${TARGET_LABELS[$BINARY_TARGET_INDEX]}"
    metadata_file="$BACKUP_DIR/${TARGET_LABELS[$METADATA_TARGET_INDEX]}"
    read_encoder_metadata "$metadata_file" \
        && [ "$(checksum "$binary_file")" = "$ENCODER_METADATA_HASH" ]
}

validate_backup_dir() {
    local backup_dir="$1" backup_name index backup_file backup_member
    if [ -z "$backup_dir" ]; then
        backup_error "empty path"
        return 1
    fi
    case "$backup_dir" in
        /*) ;;
        *) backup_error "path must be absolute"; return 1 ;;
    esac
    if ! path_has_no_symlink_component "$BACKUP_ROOT" || [ ! -d "$BACKUP_ROOT" ]; then
        backup_error "backup root is missing or has a symlinked component"
        return 1
    fi
    if ! is_owned_by_current_euid "$BACKUP_ROOT"; then
        backup_error "backup root is not owned by the current effective user"
        return 1
    fi
    backup_name="${backup_dir##*/}"
    if [ "$backup_dir" != "$BACKUP_ROOT/$backup_name" ]; then
        backup_error "path escapes backup root"
        return 1
    fi
    case "$backup_name" in
        dashboard-backup-*) ;;
        *) backup_error "unexpected backup directory name"; return 1 ;;
    esac
    if ! path_has_no_symlink_component "$backup_dir" || [ ! -d "$backup_dir" ]; then
        backup_error "backup directory is missing or has a symlinked component"
        return 1
    fi
    if [ "$(stat -c '%a' -- "$backup_dir")" != "700" ]; then
        backup_error "backup directory is not mode 700"
        return 1
    fi
    if ! is_owned_by_current_euid "$backup_dir"; then
        backup_error "backup directory is not owned by the current effective user"
        return 1
    fi
    if [ -L "$backup_dir/manifest" ] || [ ! -f "$backup_dir/manifest" ]; then
        backup_error "manifest is missing or symlinked"
        return 1
    fi
    if [ "$(stat -c '%a' -- "$backup_dir/manifest")" != "600" ]; then
        backup_error "manifest is not mode 600"
        return 1
    fi
    if ! is_owned_by_current_euid "$backup_dir/manifest"; then
        backup_error "manifest is not owned by the current effective user"
        return 1
    fi
    if ! read_backup_manifest "$backup_dir/manifest"; then
        backup_error "manifest format is not recognized"
        return 1
    fi
    for index in "${BACKUP_MANIFEST_INDICES[@]}"; do
        backup_file="$backup_dir/${TARGET_LABELS[$index]}"
        if [ "${BACKUP_PRESENT[$index]}" = present ]; then
            if [ -L "$backup_file" ] || [ ! -f "$backup_file" ]; then
                backup_error "backup file is missing or symlinked: ${TARGET_LABELS[$index]}"
                return 1
            fi
            if [ "$(stat -c '%a' -- "$backup_file")" != "600" ]; then
                backup_error "backup file is not mode 600: ${TARGET_LABELS[$index]}"
                return 1
            fi
            if ! is_owned_by_current_euid "$backup_file"; then
                backup_error "backup file is not owned by the current effective user: ${TARGET_LABELS[$index]}"
                return 1
            fi
            if [ "$(checksum "$backup_file")" != "${BACKUP_HASHES[$index]}" ]; then
                backup_error "backup file checksum differs: ${TARGET_LABELS[$index]}"
                return 1
            fi
        elif [ -e "$backup_file" ] || [ -L "$backup_file" ]; then
            backup_error "unexpected backup file: ${TARGET_LABELS[$index]}"
            return 1
        fi
    done
    validate_backup_encoder_pair || {
        backup_error "encoder pair is partial, malformed, or inconsistent"
        return 1
    }
    for backup_member in "$backup_dir"/* "$backup_dir"/.[!.]* "$backup_dir"/..?*; do
        [ -e "$backup_member" ] || [ -L "$backup_member" ] || continue
        if ! backup_file_is_expected "${backup_member##*/}" || [ -L "$backup_member" ] || [ ! -f "$backup_member" ]; then
            backup_error "backup contains an unexpected or unsafe file: ${backup_member##*/}"
            return 1
        fi
    done
}

validate_current_targets_for_restore() {
    local index target presence binary_presence metadata_presence
    for index in "${INSTALL_STAGE_INDICES[@]}"; do
        target="${TARGET_PATHS[$index]}"
        path_has_no_symlink_component "$target" || return 1
        [ ! -L "$target" ] || return 1
        if [ -e "$target" ]; then
            is_managed_copy "$target" && is_owned_by_current_euid "$target" || return 1
        fi
        presence="$(backup_presence_for_index "$index")"
        if [ "$presence" = present ] && [ ! -e "$target" ]; then
            return 1
        fi
    done
    validate_current_encoder_pair || return 1
    binary_presence="$(backup_presence_for_index "$BINARY_TARGET_INDEX")"
    metadata_presence="$(backup_presence_for_index "$METADATA_TARGET_INDEX")"
    [ "$binary_presence" = "$metadata_presence" ] || return 1
    if [ "$binary_presence" = present ]; then
        [ -e "${TARGET_PATHS[$BINARY_TARGET_INDEX]}" ] \
            && [ -e "${TARGET_PATHS[$METADATA_TARGET_INDEX]}" ] || return 1
    fi
}

restore_one_target() {
    local index="$1" target backup_file staged presence
    target="${TARGET_PATHS[$index]}"
    presence="$(backup_presence_for_index "$index")"
    if [ "$presence" = absent ]; then
        if [ -e "$target" ]; then
            is_managed_copy "$target" && is_owned_by_current_euid "$target" || return 1
            rm -- "$target"
        fi
        return 0
    fi
    backup_file="$BACKUP_DIR/${TARGET_LABELS[$index]}"
    staged="$(mktemp "$(dirname "$target")/.${TARGET_LABELS[$index]}.restore.XXXXXX")" || return 1
    if ! cp -- "$backup_file" "$staged" || ! chmod "${BACKUP_MODES[$index]}" -- "$staged" || ! mv -f -- "$staged" "$target"; then
        [ ! -e "$staged" ] || rm -f -- "$staged"
        return 1
    fi
}

restore_encoder_pair() {
    local binary_presence metadata_presence
    binary_presence="$(backup_presence_for_index "$BINARY_TARGET_INDEX")"
    metadata_presence="$(backup_presence_for_index "$METADATA_TARGET_INDEX")"
    [ "$binary_presence" = "$metadata_presence" ] || return 1
    if [ "$binary_presence" = absent ]; then
        [ ! -e "${TARGET_PATHS[$BINARY_TARGET_INDEX]}" ] || rm -- "${TARGET_PATHS[$BINARY_TARGET_INDEX]}" || return 1
        [ ! -e "${TARGET_PATHS[$METADATA_TARGET_INDEX]}" ] || rm -- "${TARGET_PATHS[$METADATA_TARGET_INDEX]}" || return 1
        return 0
    fi
    restore_one_target "$BINARY_TARGET_INDEX" \
        && restore_one_target "$METADATA_TARGET_INDEX"
}

current_target_matches_recorded_identity() {
    local index="$1" target current_hash presence
    target="${TARGET_PATHS[$index]}"
    [ -f "$target" ] && [ ! -L "$target" ] && is_owned_by_current_euid "$target" || return 1
    current_hash="$(checksum "$target")"
    presence="$(backup_presence_for_index "$index")"
    if [ "$presence" = present ] && [ "$current_hash" = "${BACKUP_HASHES[$index]}" ]; then
        return 0
    fi
    if [ -n "${PREVALIDATED_HASHES[$index]}" ] && [ "$current_hash" = "${PREVALIDATED_HASHES[$index]}" ]; then
        return 0
    fi
    [ -n "${STAGED_HASHES[$index]}" ] && [ "$current_hash" = "${STAGED_HASHES[$index]}" ] \
        && { [ "${PUBLISHED[$index]}" = true ] || [ "$TRANSACTION_PHASE" = "publishing:$index" ] || [ "$TRANSACTION_RECOVERY_ENTRY_PHASE" = "publishing:$index" ]; }
}

restore_one_target_internal() {
    local index="$1" target backup_file staged presence
    target="${TARGET_PATHS[$index]}"
    presence="$(backup_presence_for_index "$index")"
    if [ "$presence" = absent ]; then
        if [ -e "$target" ] || [ -L "$target" ]; then
            current_target_matches_recorded_identity "$index" || return 1
            rm -- "$target"
        fi
        return 0
    fi
    if [ -e "$target" ] || [ -L "$target" ]; then
        current_target_matches_recorded_identity "$index" || return 1
    fi
    backup_file="$BACKUP_DIR/${TARGET_LABELS[$index]}"
    staged="$(mktemp "$(dirname "$target")/.${TARGET_LABELS[$index]}.restore.XXXXXX")" || return 1
    if ! cp -- "$backup_file" "$staged" || ! chmod "${BACKUP_MODES[$index]}" -- "$staged" || ! mv -f -- "$staged" "$target"; then
        [ ! -e "$staged" ] || rm -f -- "$staged"
        return 1
    fi
}

restore_encoder_pair_internal() {
    restore_one_target_internal "$BINARY_TARGET_INDEX" \
        && restore_one_target_internal "$METADATA_TARGET_INDEX"
}

recovery_systemctl() {
    timeout --foreground "${RECOVERY_SYSTEMCTL_TIMEOUT_S}s" systemctl --user "$@"
}

disable_managed_current_unit_for_restore() {
    local current_enabled
    case "$BACKUP_ENABLED" in
        disabled|not-found) ;;
        enabled) return 0 ;;
        *) return 1 ;;
    esac
    if [ ! -e "${TARGET_PATHS[$UNIT_TARGET_INDEX]}" ]; then
        return 0
    fi
    is_managed_copy "${TARGET_PATHS[$UNIT_TARGET_INDEX]}" || return 1
    if recovery_systemctl disable "$UNIT_NAME"; then
        return 0
    fi
    current_enabled="$(recovery_systemctl is-enabled "$UNIT_NAME" 2>/dev/null || true)"
    case "$current_enabled" in
        disabled|not-found) return 0 ;;
        *) return 1 ;;
    esac
}

restore_activation_state() {
    case "$BACKUP_ENABLED" in
        enabled) recovery_systemctl enable "$UNIT_NAME" || return 1 ;;
        disabled|not-found) ;;
        *) return 1 ;;
    esac
    case "$BACKUP_ACTIVE" in
        active) recovery_systemctl start "$UNIT_NAME" || return 1 ;;
        inactive|not-found) ;;
        *) return 1 ;;
    esac
}

record_current_target_identities() {
    local index target
    PREVALIDATED_HASHES=("" "" "" "" "" "" "" "")
    for index in "${!TARGET_PATHS[@]}"; do
        target="${TARGET_PATHS[$index]}"
        if [ -e "$target" ]; then
            [ -f "$target" ] && [ ! -L "$target" ] && is_owned_by_current_euid "$target" || return 1
            PREVALIDATED_HASHES[index]="$(checksum "$target")"
        fi
    done
}

restore_public_targets() {
    local index
    for index in "${MODULE_TARGET_INDICES[@]}"; do
        restore_one_target "$index" || return 1
    done
    restore_encoder_pair || return 1
    restore_one_target "$UNIT_TARGET_INDEX"
}

restore_internal_targets() {
    local index
    for index in "${MODULE_TARGET_INDICES[@]}"; do
        restore_one_target_internal "$index" || return 1
    done
    restore_encoder_pair_internal || return 1
    restore_one_target_internal "$UNIT_TARGET_INDEX"
}

restore_backup_bundle() {
    local stop_current="$1" restore_activation="$2"
    validate_backup_dir "$BACKUP_DIR" || return 1
    validate_current_targets_for_restore || return 1
    record_current_target_identities || return 1
    if [ "$stop_current" = true ]; then
        recovery_systemctl stop "$UNIT_NAME" || return 1
    fi
    if [ "$restore_activation" = true ]; then
        disable_managed_current_unit_for_restore || return 1
    fi
    restore_public_targets || return 1
    recovery_systemctl daemon-reload || return 1
    if [ "$restore_activation" = true ]; then
        restore_activation_state || return 1
    fi
}

restore_backup_bundle_internal() {
    local stop_current="$1" restore_activation="$2"
    TRANSACTION_PHASE=recovering
    if [ "$stop_current" = true ]; then
        recovery_systemctl stop "$UNIT_NAME" || return 1
    fi
    if [ "$restore_activation" = true ]; then
        disable_managed_current_unit_for_restore || return 1
    fi
    restore_internal_targets || return 1
    recovery_systemctl daemon-reload || return 1
    if [ "$restore_activation" = true ]; then
        restore_activation_state || return 1
    fi
}

publish_staged_target() {
    local index="$1" staged="${STAGED[$1]}"
    [ -n "$staged" ] || return 1
    TRANSACTION_PHASE="publishing:$index"
    if ! mv -f -- "$staged" "${TARGET_PATHS[$index]}"; then
        return 1
    fi
    PUBLISHED[index]=true
    STAGED[index]=""
}

publish_staged_bundle() {
    local index
    for index in "${MODULE_TARGET_INDICES[@]}"; do
        publish_staged_target "$index" || return 1
    done
    if [ "$JPEG_ENCODER" = true ]; then
        publish_staged_target "$BINARY_TARGET_INDEX" || return 1
        publish_staged_target "$METADATA_TARGET_INDEX" || return 1
    fi
    publish_staged_target "$UNIT_TARGET_INDEX"
}

arm_transaction() {
    local stop_current="$1" restore_activation="$2"
    [ -n "$BACKUP_DIR" ] || return 1
    TRANSACTION_STOP_CURRENT="$stop_current"
    TRANSACTION_RESTORE_ACTIVATION="$restore_activation"
    TRANSACTION_RECOVERY_ATTEMPTED=false
    TRANSACTION_RECOVERY_IN_PROGRESS=false
    TRANSACTION_RECOVERY_ENTRY_PHASE=""
    PUBLISHED=(false false false false false false false false)
    TRANSACTION_PHASE=armed
    TRANSACTION_ARMED=true
}

disarm_transaction() {
    TRANSACTION_ARMED=false
    TRANSACTION_PHASE=unarmed
    TRANSACTION_RECOVERY_ENTRY_PHASE=""
    TRANSACTION_STOP_CURRENT=false
    TRANSACTION_RESTORE_ACTIVATION=false
}

recover_armed_transaction() {
    if [ "$TRANSACTION_ARMED" != true ] || [ "$TRANSACTION_RECOVERY_ATTEMPTED" = true ]; then
        return 1
    fi
    TRANSACTION_RECOVERY_ATTEMPTED=true
    TRANSACTION_RECOVERY_IN_PROGRESS=true
    TRANSACTION_RECOVERY_ENTRY_PHASE="$TRANSACTION_PHASE"
    # SIGKILL cannot be trapped. Ignore later catchable signals while one bounded
    # rollback attempt restores the selected snapshot; never recursively retry it.
    trap '' HUP INT TERM
    if restore_backup_bundle_internal "$TRANSACTION_STOP_CURRENT" "$TRANSACTION_RESTORE_ACTIVATION"; then
        TRANSACTION_RECOVERY_IN_PROGRESS=false
        disarm_transaction
        return 0
    fi
    TRANSACTION_RECOVERY_IN_PROGRESS=false
    return 1
}

transaction_failure() {
    local reason="$1"
    printf 'Dashboard transaction failed: %s\n' "$reason" >&2
    if recover_armed_transaction; then
        printf 'Automatic rollback restored the prior dashboard bundle.\n' >&2
    else
        printf 'Automatic rollback failed; backup remains at %s. Restore it only after resolving the reported safety failure.\n' "$BACKUP_DIR" >&2
    fi
    exit 1
}

health_check() {
    local initial_state initial_pid final_state final_pid
    initial_state="$(systemctl --user show --property=ActiveState --value "$UNIT_NAME")" || return 1
    initial_pid="$(systemctl --user show --property=MainPID --value "$UNIT_NAME")" || return 1
    [ "$initial_state" = active ] && [[ "$initial_pid" =~ ^[1-9][0-9]*$ ]] || return 1
    sleep 3
    final_state="$(systemctl --user show --property=ActiveState --value "$UNIT_NAME")" || return 1
    final_pid="$(systemctl --user show --property=MainPID --value "$UNIT_NAME")" || return 1
    [ "$final_state" = active ] && [ "$final_pid" = "$initial_pid" ]
}

signal_status() {
    case "$1" in
        HUP) printf '129' ;;
        INT) printf '130' ;;
        TERM) printf '143' ;;
        *) printf '1' ;;
    esac
}

handle_transaction_signal() {
    local signal_name="$1" status
    status="$(signal_status "$signal_name")"
    trap - EXIT
    trap '' HUP INT TERM
    cleanup_staged || true
    if [ "$TRANSACTION_RECOVERY_IN_PROGRESS" = true ]; then
        printf 'Interrupted by %s while rollback was already running; no additional rollback was attempted.\n' "$signal_name" >&2
    elif [ "$TRANSACTION_ARMED" = true ]; then
        printf 'Interrupted by %s; restoring the selected dashboard snapshot.\n' "$signal_name" >&2
        if recover_armed_transaction; then
            printf 'Signal rollback restored the prior dashboard bundle.\n' >&2
        else
            printf 'Signal rollback failed; backup remains at %s.\n' "$BACKUP_DIR" >&2
        fi
    fi
    exit "$status"
}

handle_exit() {
    local status="$?"
    trap - EXIT
    trap '' HUP INT TERM
    cleanup_staged || true
    if [ "$status" -ne 0 ] && [ "$TRANSACTION_ARMED" = true ] \
        && [ "$TRANSACTION_RECOVERY_ATTEMPTED" = false ]; then
        printf 'Installer exited unexpectedly; restoring the selected dashboard snapshot.\n' >&2
        if recover_armed_transaction; then
            printf 'Exit rollback restored the prior dashboard bundle.\n' >&2
        else
            printf 'Exit rollback failed; backup remains at %s.\n' "$BACKUP_DIR" >&2
        fi
    fi
    exit "$status"
}

trap handle_exit EXIT
trap 'handle_transaction_signal HUP' HUP
trap 'handle_transaction_signal INT' INT
trap 'handle_transaction_signal TERM' TERM

preflight_user_storage_roots

uninstall_bundle() {
    local index
    preflight_targets
    if [ -e "${TARGET_PATHS[$UNIT_TARGET_INDEX]}" ]; then
        systemctl --user disable --now "$UNIT_NAME"
        rm -- "${TARGET_PATHS[$UNIT_TARGET_INDEX]}"
        systemctl --user daemon-reload
    fi
    for index in "${MODULE_TARGET_INDICES[@]}"; do
        if [ -e "${TARGET_PATHS[$index]}" ]; then
            rm -- "${TARGET_PATHS[$index]}"
        fi
    done
    if [ -e "${TARGET_PATHS[$BINARY_TARGET_INDEX]}" ]; then
        rm -- "${TARGET_PATHS[$BINARY_TARGET_INDEX]}"
        rm -- "${TARGET_PATHS[$METADATA_TARGET_INDEX]}"
        rmdir -- "$ENCODER_DIR" 2>/dev/null || true
    fi
    printf 'Removed managed Thermalright dashboard user files; backups were kept.\n'
}

if [ "$MODE" = uninstall ]; then
    uninstall_bundle
    exit 0
fi

if [ "$MODE" = rollback ]; then
    BACKUP_DIR="$ROLLBACK_DIR"
    validate_backup_dir "$BACKUP_DIR" || die "Rollback refused before service mutation."
    validate_current_targets_for_restore || die "Rollback refused: every current target must be an exact managed copy."
    arm_transaction true true || die "Rollback could not arm its transaction."
    restore_backup_bundle true true || transaction_failure "manual rollback did not restore the selected snapshot"
    disarm_transaction
    printf 'Rolled back managed Thermalright dashboard bundle from %s.\n' "$BACKUP_DIR"
    exit 0
fi

preflight_sources_and_venv
ensure_target_directories
preflight_targets
if [ "$JPEG_ENCODER" = true ]; then
    ensure_encoder_directory
fi
stage_bundle
stage_encoder_pair
capture_service_state
create_backup
validate_backup_dir "$BACKUP_DIR" || die "Created backup did not pass integrity validation."
record_current_target_identities || die "Current managed target identities could not be recorded."
printf 'BACKUP_DIR=%s\n' "$BACKUP_DIR"

if [ "$START" = true ]; then
    arm_transaction true true || die "Installation could not arm its transaction."
else
    arm_transaction false false || die "Installation could not arm its transaction."
fi

if [ "$START" = true ] && [ "$BACKUP_ACTIVE" = active ]; then
    if ! systemctl --user stop "$UNIT_NAME"; then
        transaction_failure "the active unit could not be stopped before publication"
    fi
fi

if ! publish_staged_bundle; then
    transaction_failure "atomic bundle publication failed"
fi
if ! systemctl --user daemon-reload; then
    transaction_failure "user-manager daemon-reload failed"
fi

if [ "$START" = true ]; then
    if ! systemctl --user enable "$UNIT_NAME"; then
        transaction_failure "enabling the completed bundle failed"
    fi
    if ! systemctl --user start "$UNIT_NAME"; then
        transaction_failure "starting the completed bundle failed"
    fi
    if ! health_check; then
        transaction_failure "the completed bundle did not remain active with a stable MainPID during the 3-second grace period"
    fi
    disarm_transaction
    printf 'Installed, enabled, and started %s with the completed cinematic bundle.\n' "$UNIT_NAME"
else
    disarm_transaction
    printf 'Installed %s only; no service state was changed.\n' "$UNIT_NAME"
fi
