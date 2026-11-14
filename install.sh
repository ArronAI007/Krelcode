#!/usr/bin/env bash
# Krelcode 一键安装器：用户无需手动创建或管理 Python 虚拟环境。
set -euo pipefail

APP_NAME="krelcode"
DEFAULT_REPO="https://github.com/ArronAI007/Krelcode.git"
INSTALL_ROOT="${KRELCODE_INSTALL_ROOT:-${XDG_DATA_HOME:-$HOME/.local/share}/krelcode}"
BIN_DIR="${KRELCODE_BIN_DIR:-$HOME/.local/bin}"
PYTHON_SPEC="${KRELCODE_PYTHON_VERSION:-3.12}"
PYTHON_MIRROR="${KRELCODE_PYTHON_MIRROR:-}"
SOURCE=""
SOURCE_EXPLICIT=0
TAG=""
USE_UV=1
MODIFY_PATH=1
FORCE=0
DRY_RUN=0
ACTION="install"

PATH_START="# >>> krelcode installer >>>"
PATH_END="# <<< krelcode installer <<<"

usage() {
    cat <<'EOF'
Krelcode 安装器

用法:
  curl -fsSL https://raw.githubusercontent.com/ArronAI007/Krelcode/main/install.sh | bash
  ./install.sh

默认会自动完成所有安装步骤：
  - 查找系统、conda、pyenv、asdf、mise 或 uv 管理的 Python 3.11+
  - 创建 Krelcode 私有运行环境，不需要手动创建或激活 venv
  - 安装 krelcode 命令，并自动配置当前 shell 的 PATH

常用选项:
  --uninstall           卸载程序；保留 ~/.krelcode 用户配置
  --upgrade             重新安装/升级
  --dry-run             只打印安装计划，不修改文件
  -h, --help            显示帮助

高级选项（通常不需要）:
  --source PATH|URL     指定安装源
  --tag TAG             从 GitHub 安装指定 tag，如 --tag v0.58.0
  --home PATH           私有运行环境目录
  --bin-dir PATH        可执行文件目录
  --python VERSION|PATH 指定 Python 版本或解释器
  --python-mirror URL   指定 uv 下载 Python 的镜像
  --no-uv               禁止使用 uv 自动准备 Python
  --no-modify-path      不修改 shell PATH
  --force               覆盖非 Krelcode 管理的同名命令

环境变量:
  KRELCODE_PYTHON
  KRELCODE_PYTHON_VERSION
  KRELCODE_PYTHON_MIRROR
  KRELCODE_INSTALL_ROOT
  KRELCODE_BIN_DIR
EOF
}

log() { printf '[krelcode] %s\n' "$*"; }
die() { printf '[krelcode] 错误: %s\n' "$*" >&2; exit 1; }

need_value() {
    [[ $# -ge 2 && -n "$2" ]] || die "$1 缺少参数"
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --source) need_value "$@"; SOURCE="$2"; SOURCE_EXPLICIT=1; shift 2 ;;
        --tag) need_value "$@"; TAG="$2"; shift 2 ;;
        --home) need_value "$@"; INSTALL_ROOT="$2"; shift 2 ;;
        --bin-dir) need_value "$@"; BIN_DIR="$2"; shift 2 ;;
        --python) need_value "$@"; PYTHON_SPEC="$2"; shift 2 ;;
        --python-mirror) need_value "$@"; PYTHON_MIRROR="$2"; shift 2 ;;
        --no-uv) USE_UV=0; shift ;;
        --no-modify-path) MODIFY_PATH=0; shift ;;
        --force) FORCE=1; shift ;;
        --upgrade) ACTION="install"; shift ;;
        --uninstall) ACTION="uninstall"; shift ;;
        --dry-run) DRY_RUN=1; shift ;;
        -h|--help) usage; exit 0 ;;
        *) die "未知参数: $1（用 --help 查看用法）" ;;
    esac
done

# 展开 ~，并转换为绝对路径；文件尚未创建也保持稳定输出。
expand_path() {
    local value="$1"
    case "$value" in
        "~") value="$HOME" ;;
        "~/"*) value="$HOME/${value#~/}" ;;
    esac
    case "$value" in
        /*) printf '%s' "$value" ;;
        *) printf '%s/%s' "$PWD" "$value" ;;
    esac
}

INSTALL_ROOT="$(expand_path "$INSTALL_ROOT")"
BIN_DIR="$(expand_path "$BIN_DIR")"
[[ -n "$INSTALL_ROOT" && "$INSTALL_ROOT" != "/" && "$INSTALL_ROOT" != "$HOME" ]] \
    || die "拒绝使用不安全的安装目录: $INSTALL_ROOT"

try_python() {
    local candidate="$1"
    local resolved
    [[ -n "$candidate" ]] || return 1
    if [[ "$candidate" == */* ]]; then
        [[ -x "$candidate" ]] || return 1
        resolved="$candidate"
    else
        resolved="$(command -v "$candidate" 2>/dev/null || true)"
        [[ -n "$resolved" ]] || return 1
    fi
    "$resolved" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)' \
        >/dev/null 2>&1 || return 1
    printf '%s' "$resolved"
}

find_python() {
    local requested="${1:-}"
    local candidates=()
    local candidate resolved base root uv_python_dir

    # 1) 显式解释器、用户请求的版本，以及 PATH 中的常见命令。
    [[ -n "${KRELCODE_PYTHON:-}" ]] && candidates+=("$KRELCODE_PYTHON")
    if [[ -n "$requested" ]]; then
        case "$requested" in
            */*) candidates+=("$requested") ;;
            python*) candidates+=("$requested") ;;
            [0-9]*) candidates+=("python$requested") ;;
            *) candidates+=("$requested") ;;
        esac
    fi
    candidates+=(python3.14 python3.13 python3.12 python3.11 python3 python)
    for candidate in "${candidates[@]}"; do
        if resolved="$(try_python "$candidate")"; then
            printf '%s' "$resolved"
            return 0
        fi
    done

    # 2) 当前 shell 与版本管理器已经知道的环境。
    candidates=("")
    [[ -n "${VIRTUAL_ENV:-}" ]] && candidates+=("$VIRTUAL_ENV/bin/python")
    [[ -n "${CONDA_PREFIX:-}" ]] && candidates+=("$CONDA_PREFIX/bin/python")
    if [[ -n "${CONDA_EXE:-}" && "${CONDA_EXE%/bin/conda}" != "$CONDA_EXE" ]]; then
        candidates+=("${CONDA_EXE%/bin/conda}/bin/python")
    fi
    uv_python_dir="${UV_PYTHON_INSTALL_DIR:-$HOME/.local/share/uv/python}"
    for candidate in "$uv_python_dir"/*/bin/python3 "$uv_python_dir"/*/bin/python \
                     "$INSTALL_ROOT"/python/*/bin/python3 "$INSTALL_ROOT"/python/*/bin/python; do
        [[ -e "$candidate" ]] && candidates+=("$candidate")
    done
    for root in "$HOME/.pyenv/versions" "$HOME/.asdf/installs/python" \
                "$HOME/.local/share/mise/installs/python"; do
        for candidate in "$root"/*/bin/python "$root"/*/bin/python3; do
            [[ -e "$candidate" ]] && candidates+=("$candidate")
        done
    done
    for candidate in "${candidates[@]}"; do
        if resolved="$(try_python "$candidate")"; then
            printf '%s' "$resolved"
            return 0
        fi
    done

    # 3) 通过已安装的管理工具动态查询，而不是假设固定安装目录。
    if [[ -n "${UV_BIN:-}" ]]; then
        for base in 3.14 3.13 3.12 3.11 3; do
            resolved="$(UV_PYTHON_INSTALL_DIR="$INSTALL_ROOT/python" \
                UV_PYTHON_DOWNLOADS=never "$UV_BIN" python find "$base" \
                --no-project --no-cache 2>/dev/null || true)"
            if resolved="$(try_python "$resolved")"; then
                printf '%s' "$resolved"
                return 0
            fi
        done
    fi
    if command -v conda >/dev/null 2>&1; then
        base="$(conda info --base 2>/dev/null || true)"
        if resolved="$(try_python "$base/bin/python")"; then
            printf '%s' "$resolved"
            return 0
        fi
    fi
    if command -v pyenv >/dev/null 2>&1; then
        resolved="$(pyenv which python 2>/dev/null || true)"
        if resolved="$(try_python "$resolved")"; then
            printf '%s' "$resolved"
            return 0
        fi
    fi
    if command -v asdf >/dev/null 2>&1; then
        resolved="$(asdf where python 2>/dev/null || true)/bin/python"
        if resolved="$(try_python "$resolved")"; then
            printf '%s' "$resolved"
            return 0
        fi
    fi
    if command -v mise >/dev/null 2>&1; then
        resolved="$(mise which python 2>/dev/null || true)"
        if resolved="$(try_python "$resolved")"; then
            printf '%s' "$resolved"
            return 0
        fi
    fi
    if command -v brew >/dev/null 2>&1; then
        for base in 3.14 3.13 3.12 3.11; do
            resolved="$(brew --prefix "python@$base" 2>/dev/null || true)/bin/python3"
            if resolved="$(try_python "$resolved")"; then
                printf '%s' "$resolved"
                return 0
            fi
        done
        resolved="$(brew --prefix python3 2>/dev/null || true)/bin/python3"
        if resolved="$(try_python "$resolved")"; then
            printf '%s' "$resolved"
            return 0
        fi
    fi

    # 4) 常见路径只作为最后兜底。
    candidates=(
        /opt/homebrew/bin/python3 /usr/local/bin/python3
        /opt/anaconda3/bin/python /opt/conda/bin/python
        "$HOME/anaconda3/bin/python" "$HOME/miniconda3/bin/python"
        "$HOME/miniforge3/bin/python" /usr/bin/python3
    )
    for candidate in "${candidates[@]}"; do
        if resolved="$(try_python "$candidate")"; then
            printf '%s' "$resolved"
            return 0
        fi
    done
    return 1
}

find_uv() {
    local candidate
    for candidate in "${KRELCODE_UV:-}" uv "$HOME/.local/bin/uv" \
        "$HOME/.cargo/bin/uv" "$INSTALL_ROOT/bootstrap/uv" \
        "$INSTALL_ROOT/bootstrap/bin/uv" "$INSTALL_ROOT"/bootstrap/*/uv; do
        [[ -n "$candidate" ]] || continue
        if [[ "$candidate" == */* ]]; then
            [[ -x "$candidate" ]] && printf '%s' "$candidate" && return 0
        elif command -v "$candidate" >/dev/null 2>&1; then
            command -v "$candidate"
            return 0
        fi
    done
    return 1
}

find_krelcode_python() {
    local candidate resolved
    for candidate in "$INSTALL_ROOT"/python/*/bin/python3 \
                     "$INSTALL_ROOT"/python/*/bin/python3.* \
                     "$INSTALL_ROOT"/python/*/bin/python; do
        if resolved="$(try_python "$candidate")"; then
            printf '%s' "$resolved"
            return 0
        fi
    done
    if [[ -n "${UV_BIN:-}" ]]; then
        resolved="$(UV_PYTHON_INSTALL_DIR="$INSTALL_ROOT/python" \
            UV_PYTHON_DOWNLOADS=never "$UV_BIN" python find "$PYTHON_SPEC" \
            --no-project --no-cache 2>/dev/null || true)"
        if resolved="$(try_python "$resolved")"; then
            printf '%s' "$resolved"
            return 0
        fi
    fi
    return 1
}

ensure_uv() {
    [[ -n "$UV_BIN" ]] && return 0
    local installer_url="https://astral.sh/uv/install.sh"
    mkdir -p "$INSTALL_ROOT/bootstrap"
    log "正在准备 Python 运行组件..."
    if command -v curl >/dev/null 2>&1; then
        curl -LsSf "$installer_url" | \
            env UV_INSTALL_DIR="$INSTALL_ROOT/bootstrap" UV_NO_MODIFY_PATH=1 sh \
            >/dev/null 2>&1 || true
    elif command -v wget >/dev/null 2>&1; then
        wget -qO- "$installer_url" | \
            env UV_INSTALL_DIR="$INSTALL_ROOT/bootstrap" UV_NO_MODIFY_PATH=1 sh \
            >/dev/null 2>&1 || true
    fi
    UV_BIN="$(find_uv || true)"
    [[ -n "$UV_BIN" ]]
}

prepare_uv_python() {
    ensure_uv || return 1
    log "正在自动准备 Python $PYTHON_SPEC..."
    local args
    args=(python install --no-progress --no-bin)
    if [[ -n "$PYTHON_MIRROR" ]]; then
        args+=(--mirror "$PYTHON_MIRROR")
    fi
    UV_PYTHON_INSTALL_DIR="$INSTALL_ROOT/python" \
    UV_CACHE_DIR="$INSTALL_ROOT/cache/uv" \
        "$UV_BIN" "${args[@]}" "$PYTHON_SPEC" >/dev/null 2>&1 || return 1
    LOCAL_PYTHON="$(find_krelcode_python || true)"
    [[ -n "$LOCAL_PYTHON" ]]
}

create_private_venv() {
    local python_bin="$1"
    rm -rf "$INSTALL_ROOT/venv"
    "$python_bin" -m venv "$INSTALL_ROOT/venv" >/dev/null 2>&1 || return 1
    [[ -x "$INSTALL_ROOT/venv/bin/python" ]]
}

default_source() {
    local here=""
    if [[ -n "${BASH_SOURCE[0]:-}" && -f "${BASH_SOURCE[0]}" ]]; then
        here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
        if [[ -f "$here/pyproject.toml" ]] && \
           grep -Eq '^name[[:space:]]*=[[:space:]]*"krelcode"' "$here/pyproject.toml"; then
            printf '%s' "$here"
            return
        fi
    fi
    printf 'git+%s' "$DEFAULT_REPO"
}

if [[ -z "$SOURCE" ]]; then
    SOURCE="$(default_source)"
fi
if [[ -n "$TAG" ]]; then
    [[ "$SOURCE_EXPLICIT" == 0 ]] || die "不要同时使用 --source 和 --tag；请在 --source 中写完整版本"
    SOURCE="git+${DEFAULT_REPO}@${TAG}"
fi

# 若当前目录是源码树，但用户传了相对 --source，先转绝对路径。
if [[ -d "$SOURCE" ]]; then
    SOURCE="$(cd "$SOURCE" && pwd)"
fi

UV_BIN=""
if [[ "$USE_UV" == 1 ]]; then
    UV_BIN="$(find_uv || true)"
fi

if [[ "$PYTHON_SPEC" == */* ]]; then
    LOCAL_PYTHON="$(try_python "$PYTHON_SPEC")" || \
        die "--python 指定的解释器不存在或版本低于 3.11: $PYTHON_SPEC"
elif [[ -n "${KRELCODE_PYTHON:-}" ]]; then
    LOCAL_PYTHON="$(try_python "$KRELCODE_PYTHON")" || \
        die "KRELCODE_PYTHON 指定的解释器不存在或版本低于 3.11: $KRELCODE_PYTHON"
else
    LOCAL_PYTHON="$(find_python "$PYTHON_SPEC" || true)"
fi

if [[ "$DRY_RUN" == 1 ]]; then
    if [[ "$ACTION" == "uninstall" ]]; then
        printf 'action=uninstall\ninstall_root=%s\nbin_dir=%s\n' "$INSTALL_ROOT" "$BIN_DIR"
        exit 0
    fi
    if [[ -n "$LOCAL_PYTHON" ]]; then
        python_origin="system"
    else
        python_origin="uv-auto"
    fi
    printf 'action=install\nbackend=private-venv\nsource=%s\ninstall_root=%s\nbin_dir=%s\npython=%s\npython_origin=%s\nlocal_python=%s\npython_mirror=%s\n' \
        "$SOURCE" "$INSTALL_ROOT" "$BIN_DIR" "$PYTHON_SPEC" "$python_origin" \
        "${LOCAL_PYTHON:-none}" "${PYTHON_MIRROR:-default}"
    exit 0
fi

profile_for_shell() {
    local shell_name
    shell_name="$(basename "${SHELL:-}")"
    case "$shell_name" in
        zsh) printf '%s' "${ZDOTDIR:-$HOME}/.zshrc" ;;
        bash)
            if [[ "$(uname -s)" == "Darwin" ]]; then
                printf '%s' "$HOME/.bash_profile"
            else
                printf '%s' "$HOME/.bashrc"
            fi
            ;;
        fish) printf '%s' "$HOME/.config/fish/conf.d/krelcode.fish" ;;
        *) printf '%s' "$HOME/.profile" ;;
    esac
}

remove_path_block() {
    local profile="$1"
    [[ -f "$profile" ]] || return 0
    grep -Fq "$PATH_START" "$profile" || return 0
    local tmp
    tmp="$(mktemp "${profile}.krelcode.XXXXXX")" || return 1
    awk -v start="$PATH_START" -v end="$PATH_END" '
        $0 == start { skip=1; next }
        $0 == end { skip=0; next }
        !skip { print }
    ' "$profile" > "$tmp"
    mv "$tmp" "$profile"
}

add_path_block() {
    local profile
    profile="$(profile_for_shell)"
    mkdir -p "$(dirname "$profile")"
    if grep -Fq "$PATH_START" "$profile" 2>/dev/null; then
        return 0
    fi
    local quoted
    if [[ "$(basename "${SHELL:-}")" == "fish" ]]; then
        local fish_path="${BIN_DIR//\'/\'\\\'\'}"
        {
            printf '\n%s\n' "$PATH_START"
            printf "fish_add_path --prepend '%s'\n" "$fish_path"
            printf '%s\n' "$PATH_END"
        } >> "$profile"
    else
        quoted="$(printf '%q' "$BIN_DIR")"
        {
            printf '\n%s\n' "$PATH_START"
            printf 'export PATH=%s:"$PATH"\n' "$quoted"
            printf '%s\n' "$PATH_END"
        } >> "$profile"
    fi
    log "已将 $BIN_DIR 写入 $profile（新终端生效）"
}

remove_managed_command() {
    local command_path="$BIN_DIR/$APP_NAME"
    [[ -e "$command_path" || -L "$command_path" ]] || return 0
    if [[ -L "$command_path" ]]; then
        local target target_abs target_dir
        target="$(readlink "$command_path")"
        target_abs="$target"
        [[ "$target_abs" == /* ]] || target_abs="$BIN_DIR/$target_abs"
        target_dir="$(cd "$(dirname "$target_abs")" 2>/dev/null && pwd || true)"
        [[ -n "$target_dir" ]] && target_abs="$target_dir/$(basename "$target_abs")"
        case "$target_abs" in
            "$INSTALL_ROOT"/*) rm -f "$command_path" ;;
            *) log "保留非本安装器管理的命令: $command_path -> $target" ;;
        esac
    else
        log "保留非符号链接命令: $command_path（请确认来源后手动删除）"
    fi
}

uninstall_krelcode() {
    if [[ -d "$INSTALL_ROOT/uv" ]] && command -v uv >/dev/null 2>&1; then
        UV_TOOL_DIR="$INSTALL_ROOT/uv/tools" UV_TOOL_BIN_DIR="$BIN_DIR" \
            uv tool uninstall "$APP_NAME" >/dev/null 2>&1 || true
    fi
    remove_managed_command
    remove_path_block "$(profile_for_shell)"
    remove_path_block "$HOME/.profile"
    remove_path_block "$HOME/.bashrc"
    remove_path_block "$HOME/.bash_profile"
    remove_path_block "${ZDOTDIR:-$HOME}/.zshrc"
    remove_path_block "$HOME/.config/fish/conf.d/krelcode.fish"
    rm -rf "$INSTALL_ROOT"
    log "已卸载 Krelcode；用户配置 ~/.krelcode 未删除"
}

if [[ "$ACTION" == "uninstall" ]]; then
    uninstall_krelcode
    exit 0
fi

mkdir -p "$INSTALL_ROOT" "$BIN_DIR"

# 默认路径：复用系统/版本管理器中的 Python，并创建 Krelcode 私有环境。
if [[ -z "$LOCAL_PYTHON" ]]; then
    if [[ "$USE_UV" != 1 ]]; then
        die "系统未找到 Python 3.11+，且 uv 自动准备已禁用。"
    fi
    prepare_uv_python || die \
        "无法自动准备 Python 3.11+。请检查网络后重试。"
fi

if ! create_private_venv "$LOCAL_PYTHON"; then
    if [[ "$USE_UV" != 1 ]]; then
        die "当前 Python 无法创建私有环境；请先确认 python3 -m venv 可用。"
    fi
    log "当前 Python 无法创建独立环境，正在自动准备专用 Python..."
    prepare_uv_python || die "无法自动准备可用的 Python 3.11+。"
    create_private_venv "$LOCAL_PYTHON" || die "无法创建 Krelcode 私有 Python 环境。"
fi

PYTHON_BIN="$LOCAL_PYTHON"
PYTHON_VERSION="$("$PYTHON_BIN" -c 'import platform; print(platform.python_version())')"
log "使用 Python $PYTHON_VERSION 安装（已自动创建独立环境）"
VENV_PYTHON="$INSTALL_ROOT/venv/bin/python"
ENTRY="$INSTALL_ROOT/venv/bin/$APP_NAME"
PROJECT_SOURCE=""
if [[ -d "$SOURCE" && -f "$SOURCE/pyproject.toml" && -d "$SOURCE/src/krelcode" ]]; then
    PROJECT_SOURCE="$SOURCE"
elif [[ "$SOURCE" == git+* ]]; then
    command -v git >/dev/null 2>&1 || die "从 GitHub 安装需要 git，请先安装 git。"
    GIT_SPEC="${SOURCE#git+}"
    GIT_REPO="$GIT_SPEC"
    GIT_TAG=""
    if [[ "$GIT_SPEC" == *".git@"* ]]; then
        GIT_REPO="${GIT_SPEC%.git@*}.git"
        GIT_TAG="${GIT_SPEC##*.git@}"
    fi
    CLONE_DIR="$INSTALL_ROOT/source"
    rm -rf "$CLONE_DIR"
    if [[ -n "$GIT_TAG" ]]; then
        git clone --quiet --depth 1 --branch "$GIT_TAG" "$GIT_REPO" "$CLONE_DIR"
    else
        git clone --quiet --depth 1 "$GIT_REPO" "$CLONE_DIR"
    fi
    PROJECT_SOURCE="$CLONE_DIR"
fi

if [[ -n "$PROJECT_SOURCE" ]]; then
    [[ -f "$PROJECT_SOURCE/pyproject.toml" && -d "$PROJECT_SOURCE/src/krelcode" ]] \
        || die "源码目录结构无效: $PROJECT_SOURCE"
    SITE_PACKAGES="$("$VENV_PYTHON" -c 'import sysconfig; print(sysconfig.get_paths()["purelib"])')"
    rm -rf "$SITE_PACKAGES/krelcode"
    cp -R "$PROJECT_SOURCE/src/krelcode" "$SITE_PACKAGES/krelcode"
    find "$SITE_PACKAGES/krelcode" -type d -name __pycache__ -prune -exec rm -rf {} +
    find "$SITE_PACKAGES/krelcode" -type f -name '*.pyc' -delete
    {
        printf '#!/bin/sh\n'
        printf 'exec %s -m krelcode "$@"\n' "$(printf '%q' "$VENV_PYTHON")"
    } > "$ENTRY"
    chmod +x "$ENTRY"
else
    PIP_CACHE_DIR="$INSTALL_ROOT/cache/pip" \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
        "$VENV_PYTHON" -m pip install --no-input --upgrade --force-reinstall "$SOURCE"
fi

[[ -x "$ENTRY" ]] || die "安装完成但未生成 $ENTRY"
COMMAND_PATH="$BIN_DIR/$APP_NAME"
if [[ -e "$COMMAND_PATH" || -L "$COMMAND_PATH" ]]; then
    if [[ "$FORCE" != 1 ]]; then
        target="$(readlink "$COMMAND_PATH" 2>/dev/null || true)"
        case "$target" in
            "$INSTALL_ROOT"/*) ;;
            *) die "$COMMAND_PATH 已存在且非本安装器管理；确认可覆盖后使用 --force" ;;
        esac
    fi
fi
ln -sfn "$ENTRY" "$COMMAND_PATH"
BACKEND="private-venv"

printf 'backend=%s\nsource=%s\ninstalled_at=%s\n' \
    "$BACKEND" "$SOURCE" "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" \
    > "$INSTALL_ROOT/install.env"

BIN_DIR_ON_PATH=0
if [[ ":$PATH:" == *":$BIN_DIR:"* ]]; then
    BIN_DIR_ON_PATH=1
fi
export PATH="$BIN_DIR:$PATH"
VERSION_OUTPUT="$("$BIN_DIR/$APP_NAME" --version)"
log "安装完成: $VERSION_OUTPUT"

if [[ "$BIN_DIR_ON_PATH" == 0 ]]; then
    if [[ "$MODIFY_PATH" == 1 ]]; then
        add_path_block
    else
        log "请把 $BIN_DIR 加入 PATH：export PATH=\"$BIN_DIR:\$PATH\""
    fi
fi
log "当前脚本内验证通过；新终端可直接运行: krelcode"
