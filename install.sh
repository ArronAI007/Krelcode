#!/usr/bin/env bash
# Krelcode 一键安装器：用户无需手动创建或管理 Python 虚拟环境。
set -euo pipefail

APP_NAME="krelcode"
DEFAULT_REPO="https://github.com/ArronAI007/Krelcode.git"
INSTALL_ROOT="${KRELCODE_INSTALL_ROOT:-${XDG_DATA_HOME:-$HOME/.local/share}/krelcode}"
BIN_DIR="${KRELCODE_BIN_DIR:-$HOME/.local/bin}"
PYTHON_SPEC="${KRELCODE_PYTHON_VERSION:-3.12}"
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
Krelcode 全局安装器

用法:
  ./install.sh [选项]
  curl -fsSL https://raw.githubusercontent.com/ArronAI007/Krelcode/main/install.sh | bash

选项:
  --source PATH|URL     安装源（默认自动选择当前源码树或 GitHub 仓库）
  --tag TAG             从 GitHub 安装指定 tag，如 --tag v0.57.0
  --home PATH           私有运行环境目录（默认 ~/.local/share/krelcode）
  --bin-dir PATH        可执行文件目录（默认 ~/.local/bin）
  --python VERSION      uv 使用的 Python 版本（默认 3.12）
  --no-uv               不使用 uv，改用私有的标准库 venv 环境
  --no-modify-path      不自动向 shell 配置写入 PATH
  --force               覆盖非 Krelcode 管理的同名命令
  --upgrade             重新安装当前安装源（可与默认源码树/GitHub 配合）
  --uninstall           卸载程序；保留 ~/.krelcode 用户配置
  --dry-run             只打印安装计划，不修改文件
  -h, --help            显示帮助

安装器会自行创建 Krelcode 私有环境。用户不需要运行 python -m venv，
也不需要手动激活环境；安装完成后直接运行 krelcode。
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

find_python() {
    local candidate
    for candidate in "${KRELCODE_PYTHON:-}" python3 python; do
        [[ -n "$candidate" ]] || continue
        if command -v "$candidate" >/dev/null 2>&1 && \
           "$candidate" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)' >/dev/null 2>&1; then
            command -v "$candidate"
            return 0
        fi
    done
    return 1
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
if [[ "$USE_UV" == 1 ]] && command -v uv >/dev/null 2>&1; then
    UV_BIN="$(command -v uv)"
fi

if [[ "$DRY_RUN" == 1 ]]; then
    if [[ "$ACTION" == "uninstall" ]]; then
        printf 'action=uninstall\ninstall_root=%s\nbin_dir=%s\n' "$INSTALL_ROOT" "$BIN_DIR"
        exit 0
    fi
    if [[ -n "$UV_BIN" ]]; then
        backend="uv-tool"
    else
        backend="private-venv"
    fi
    printf 'action=install\nbackend=%s\nsource=%s\ninstall_root=%s\nbin_dir=%s\npython=%s\n' \
        "$backend" "$SOURCE" "$INSTALL_ROOT" "$BIN_DIR" "$PYTHON_SPEC"
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

if [[ -n "$UV_BIN" ]]; then
    log "使用 uv tool 安装（Krelcode 环境由 uv 隔离管理）"
    if ! UV_TOOL_DIR="$INSTALL_ROOT/uv/tools" \
         UV_TOOL_BIN_DIR="$BIN_DIR" \
         UV_CACHE_DIR="$INSTALL_ROOT/cache/uv" \
            "$UV_BIN" tool install --force --no-progress \
            --python "$PYTHON_SPEC" "$SOURCE"; then
        log "uv 安装失败，回退到 Krelcode 私有 venv"
        UV_BIN=""
    else
        BACKEND="uv-tool"
    fi
fi

if [[ -z "$UV_BIN" ]]; then
    PYTHON_BIN="$(find_python)" || die \
        "未找到 Python 3.11+，也没有 uv。请安装 Python 3.11+ 或 uv 后重试。"
    log "使用 Krelcode 私有 venv 安装（用户无需手动激活）"
    rm -rf "$INSTALL_ROOT/venv"
    "$PYTHON_BIN" -m venv "$INSTALL_ROOT/venv"
    VENV_PYTHON="$INSTALL_ROOT/venv/bin/python"
    [[ -x "$VENV_PYTHON" ]] || die "无法创建私有 Python 环境: $INSTALL_ROOT/venv"
    ENTRY="$INSTALL_ROOT/venv/bin/$APP_NAME"
    PROJECT_SOURCE=""
    if [[ -d "$SOURCE" && -f "$SOURCE/pyproject.toml" && -d "$SOURCE/src/krelcode" ]]; then
        PROJECT_SOURCE="$SOURCE"
    elif [[ "$SOURCE" == git+* ]]; then
        command -v git >/dev/null 2>&1 || die "Git 安装源需要 git（或先安装 uv）"
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
        log "已从源码安装纯 Python 包（无需构建依赖）"
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
fi

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
