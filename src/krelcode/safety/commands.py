"""shell 命令风险分级（纯函数，可全面单测）。

方法：
1. 原文预检（fork 炸弹等 shlex 无法解析的模式）
2. shlex 切 token，按控制符（&& || ; | & 重定向 括号）切成独立段
3. 每段按「程序名 + 参数」分级：DENY > ASK > ALLOW，整条命令取最严格
4. 跨段规则：下载器(curl/wget) 管道给解释器(sh/python…) → DENY
5. 出现重定向（写文件）→ 至少 ASK

保守原则：不认识的一律 ASK；解析失败也 ASK（交由审批兜底）。
"""

from __future__ import annotations

import shlex
from typing import List, Sequence, Tuple

RISK_ALLOW = "allow"
RISK_ASK = "ask"
RISK_DENY = "deny"

_RISK_ORDER = {RISK_ALLOW: 0, RISK_ASK: 1, RISK_DENY: 2}

SEPARATORS = {"&&", "||", ";", "|", "&", "(", ")"}
REDIRECTS = {">", ">>", "<", "<<", "<<<", ">|", "&>", "&>>", ">&", "<&", "<>"}

# 只读命令白名单（无副作用）
SAFE_PROGRAMS = {
    "ls", "cat", "pwd", "echo", "head", "tail", "wc", "find", "grep", "rg",
    "which", "whoami", "date", "env", "printenv", "tree", "file", "stat",
    "du", "df", "ps", "uname", "basename", "dirname", "sort", "uniq", "cut",
    "tr", "diff", "md5", "shasum", "true", "false", "id", "hostname",
}

FIND_ACTION_OPTIONS = {
    "-exec", "-execdir", "-ok", "-okdir", "-delete",
    "-fprint", "-fprint0", "-fprintf", "-fls",
}
FIND_LINK_OPTIONS = {"-L", "-H"}
GIT_OUTPUT_COMMANDS = {"log", "diff", "show", "shortlog", "whatchanged"}
GIT_BRANCH_LIST_FLAGS = {
    "-a", "-r", "-v", "-vv", "-l", "--all", "--remotes", "--list",
    "--verbose", "--show-current", "--omit-empty", "--column", "--no-column",
    "--color", "--no-color", "--ignore-case", "-i", "--no-abbrev", "--abbrev",
}
GIT_BRANCH_VALUE_FLAGS = {
    "--contains", "--no-contains", "--merged", "--no-merged", "--points-at",
    "--sort", "--format",
}
GIT_TAG_LIST_FLAGS = {
    "-l", "--list", "-n", "--column", "--no-column", "--color", "--no-color",
    "--omit-empty",
}
GIT_TAG_VALUE_FLAGS = {
    "--contains", "--no-contains", "--merged", "--no-merged", "--points-at",
    "--sort", "--format",
}

# git 只读子命令
SAFE_GIT_SUBCOMMANDS = {
    "status", "log", "diff", "show", "branch", "rev-parse", "describe",
    "ls-files", "cat-file", "shortlog", "tag", "remote", "blame",
}

# 无条件拒绝的程序
DENY_PROGRAMS = {
    "sudo", "su", "doas",           # 提权
    "mkfs", "dd", "diskutil",       # 磁盘破坏
    "shutdown", "reboot", "halt", "poweroff",
}

# 会执行任意代码的解释器/构建器 → ASK
CODE_RUNNERS = {
    "python", "python3", "node", "ruby", "perl", "php", "sh", "bash", "zsh",
    "pytest", "npm", "yarn", "pnpm", "pip", "pip3", "uv", "cargo", "make",
    "go", "java", "mvn", "gradle", "docker", "kubectl",
}

SHELL_INTERPRETERS = {"sh", "bash", "zsh", "python", "python3", "node", "ruby", "perl", "eval"}
DOWNLOADERS = {"curl", "wget", "fetch"}

# rm 的灾难性目标（含前缀形式）
CATASTROPHIC_TARGETS = {
    "/", "~", "/*", "~/*", "/etc", "/usr", "/bin", "/sbin", "/var", "/home",
    "/opt", "/dev", "/proc", "/System", "/Library", "/Applications", "/Users",
    "/private", "/tmp", "/root",
}


def classify_command(raw: str) -> Tuple[str, str]:
    """返回 (risk, reason)。risk ∈ allow/ask/deny。"""
    if not raw or not raw.strip():
        return RISK_ASK, "空命令"

    # 1) 原文预检：fork 炸弹
    if ":(){" in raw.replace(" ", "") or ":(){:|:&}" in raw:
        return RISK_DENY, "疑似 fork 炸弹"

    # 2) 切 token（punctuation_chars 让 ';' '|' '&&' '>>' 等紧邻字符也独立成 token）；
    #    解析失败保守 ASK
    try:
        tokens = _tokenize(raw)
    except ValueError as e:
        return RISK_ASK, f"命令无法解析（{e}），需人工确认"
    if not tokens:
        return RISK_ASK, "空命令"

    # 3) 切段。重定向目标不再被误当成独立命令。
    segments: List[List[str]] = []
    current: List[str] = []
    has_redirect = False
    skip_redirect_target = False
    for tok in tokens:
        if skip_redirect_target and tok not in SEPARATORS and tok not in REDIRECTS:
            skip_redirect_target = False
            continue
        skip_redirect_target = False
        if tok in SEPARATORS:
            if current:
                segments.append(current)
                current = []
        elif tok in REDIRECTS:
            has_redirect = True
            if current and current[-1].isdigit():
                current = current[:-1]
            if current:
                segments.append(current)
                current = []
            skip_redirect_target = True
        else:
            current.append(tok)
    if current:
        segments.append(current)

    # 4) 跨段规则：下载器 | 解释器 → DENY
    programs = [_basename(seg[0]) for seg in segments if seg]
    for i, prog in enumerate(programs[:-1]):
        if prog in DOWNLOADERS and programs[i + 1] in SHELL_INTERPRETERS:
            return RISK_DENY, f"禁止 {prog} 管道给 {programs[i+1]} 执行（远程代码执行风险）"

    # 5) 逐段分级，取最严格
    worst = RISK_ALLOW
    reasons: List[str] = []
    for seg in segments:
        risk, reason = _classify_segment(seg)
        if _RISK_ORDER[risk] > _RISK_ORDER[worst]:
            worst = risk
        if reason and reason not in reasons:
            reasons.append(reason)
        if worst == RISK_DENY:
            break

    # 6) 重定向升级
    if has_redirect and worst == RISK_ALLOW:
        worst = RISK_ASK
        reasons.append("含输出重定向（写文件）")

    if not reasons:
        reasons.append("只读命令")
    return worst, "；".join(reasons)


def _tokenize(raw: str) -> List[str]:
    # POSIX 模式与真实 /bin/sh 一致：去掉成对引号，否则 `sh -c '...'`
    # 的嵌套命令会带着字面引号进入安全审计，绕过路径检查。
    lex = shlex.shlex(raw, posix=True, punctuation_chars=True)
    lex.whitespace_split = True
    return list(lex)


def _basename(prog: str) -> str:
    return prog.rsplit("/", 1)[-1] if "/" in prog else prog


def _is_denied_program(prog: str) -> bool:
    """mkfs.ext4 / dd.gz 之类带后缀变体也按前缀命中拒绝名单。"""
    return prog in DENY_PROGRAMS or prog.split(".")[0] in DENY_PROGRAMS


def _classify_segment(seg: Sequence[str]) -> Tuple[str, str]:
    if not seg:
        return RISK_ALLOW, ""
    prog = _basename(seg[0])
    args = list(seg[1:])

    if _is_denied_program(prog):
        return RISK_DENY, f"禁止执行 {prog}"
    if prog == "rm":
        return _classify_rm(args)
    if prog == "env":
        return _classify_env(args)
    if prog == "find":
        return _classify_find(args)
    if prog == "sort":
        return _classify_sort(args)
    if prog == "rg":
        return _classify_rg(args)
    if prog == "tree":
        return _classify_output_option(args, "tree")
    if prog == "diff":
        return _classify_output_option(args, "diff")
    if prog == "uniq":
        return _classify_uniq(args)
    if prog == "file":
        if _has_any_option(args, {"-C", "--compile"}):
            return RISK_ASK, "file -C 会写入 magic 数据库"
        return RISK_ALLOW, ""
    if prog == "date":
        positional = [a for a in args if not a.startswith("-") and not a.startswith("+")]
        if _has_any_option(args, {"-s", "--set"}) or positional:
            return RISK_ASK, "date 可能修改系统时间"
        return RISK_ALLOW, ""
    if prog == "hostname":
        if any(not a.startswith("-") for a in args):
            return RISK_ASK, "hostname 可能修改系统主机名"
        return RISK_ALLOW, ""
    if prog == "git":
        return _classify_git(args)
    if prog in DOWNLOADERS:
        return RISK_ASK, f"{prog} 访问网络"
    if prog in SAFE_PROGRAMS:
        return RISK_ALLOW, ""
    if prog in CODE_RUNNERS:
        return RISK_ASK, f"{prog} 会执行代码/修改环境"
    return RISK_ASK, f"未知命令 {prog}"


def _classify_rm(args: Sequence[str]) -> Tuple[str, str]:
    flags = [a for a in args if a.startswith("-")]
    targets = [a for a in args if not a.startswith("-")]
    flag_chars = set("".join(f.lstrip("-") for f in flags))

    for t in targets:
        normalized = t.rstrip("/") or "/"
        if t in CATASTROPHIC_TARGETS or normalized in CATASTROPHIC_TARGETS:
            return RISK_DENY, f"rm 目标过于危险: {t}"
        if t.startswith("/") and normalized.count("/") <= 1:
            return RISK_DENY, f"rm 顶层绝对路径: {t}"
    if "r" in flag_chars and not targets:
        return RISK_DENY, "rm -r 未指定目标"
    return RISK_ASK, "rm 删除文件"


def _has_any_option(args: Sequence[str], names: set[str]) -> bool:
    return any(arg in names or any(arg.startswith(f"{name}=") for name in names)
               for arg in args)


def _nested_risk(tokens: Sequence[str], label: str) -> Tuple[str, str]:
    risk, reason = classify_command(shlex.join(tokens))
    return risk, f"{label}: {reason}"


def _nested_risk_text(raw: str, label: str) -> Tuple[str, str]:
    try:
        tokens = _tokenize(raw)
    except ValueError as exc:
        return RISK_ASK, f"{label} 无法解析（{exc}），需人工确认"
    if not tokens:
        return RISK_ASK, f"{label} 为空"
    return _nested_risk(tokens, label)


def _classify_env(args: Sequence[str]) -> Tuple[str, str]:
    """env 可包装任意命令，必须按内层命令重新分级。"""
    index = 0
    while index < len(args):
        arg = args[index]
        if arg == "--":
            index += 1
            break
        if arg in {"-S", "--split-string"}:
            if index + 1 >= len(args):
                return RISK_ASK, "env -S 缺少命令字符串"
            return _nested_risk_text(args[index + 1], "env 包装命令")
        if arg.startswith("--split-string="):
            return _nested_risk_text(
                arg.split("=", 1)[1], "env 包装命令")
        if arg in {"-u", "--unset", "-C", "--chdir"}:
            index += 2
            continue
        if arg.startswith(("--unset=", "--chdir=")):
            index += 1
            continue
        if arg.startswith("-") and arg != "-":
            index += 1
            continue
        break
    while index < len(args) and re_full_assignment(args[index]):
        index += 1
    if index >= len(args):
        return RISK_ALLOW, ""
    return _nested_risk(args[index:], "env 包装命令")


def re_full_assignment(token: str) -> bool:
    name, sep, _ = token.partition("=")
    return bool(sep and name and (name[0].isalpha() or name[0] == "_")
                and all(ch.isalnum() or ch == "_" for ch in name))


def _classify_find(args: Sequence[str]) -> Tuple[str, str]:
    if any(arg in FIND_LINK_OPTIONS for arg in args):
        return RISK_ASK, "find 跟随符号链接可能越出工作区"
    for index, arg in enumerate(args):
        if arg not in FIND_ACTION_OPTIONS:
            continue
        if arg == "-delete":
            return RISK_ASK, "find -delete 会删除文件"
        if index + 1 < len(args):
            risk, reason = _nested_risk(args[index + 1:], f"find {arg}")
            if risk != RISK_ALLOW:
                return max((RISK_ASK, f"find {arg} 会执行命令"), (risk, reason),
                           key=lambda item: _RISK_ORDER[item[0]])
        return RISK_ASK, f"find {arg} 会执行命令或写文件"
    return RISK_ALLOW, ""


def _classify_sort(args: Sequence[str]) -> Tuple[str, str]:
    for index, arg in enumerate(args):
        if arg == "--compress-program":
            if index + 1 >= len(args):
                return RISK_ASK, "sort --compress-program 缺少程序"
            return _nested_risk([args[index + 1]], "sort 压缩程序")
        if arg.startswith("--compress-program="):
            return _nested_risk_text(
                arg.split("=", 1)[1], "sort 压缩程序")
    if _has_any_option(args, {"-o", "--output"}) or any(
            arg.startswith("-o") and arg != "-o" for arg in args):
        return RISK_ASK, "sort 输出选项会写文件"
    return RISK_ALLOW, ""


def _classify_rg(args: Sequence[str]) -> Tuple[str, str]:
    for index, arg in enumerate(args):
        if arg == "--pre":
            if index + 1 >= len(args):
                return RISK_ASK, "rg --pre 缺少程序"
            return _nested_risk_text(
                args[index + 1], "rg --pre 预处理程序")
        if arg.startswith("--pre="):
            return _nested_risk_text(
                arg.split("=", 1)[1], "rg --pre 预处理程序")
    return RISK_ALLOW, ""


def _classify_output_option(args: Sequence[str], program: str) -> Tuple[str, str]:
    if _has_any_option(args, {"-o", "--output"}) or any(
            arg.startswith("-o") and arg != "-o" for arg in args):
        return RISK_ASK, f"{program} 输出选项会写文件"
    return RISK_ALLOW, ""


def _classify_uniq(args: Sequence[str]) -> Tuple[str, str]:
    positionals = [arg for arg in args if arg == "--" or not arg.startswith("-")]
    if "--" in positionals:
        positionals.remove("--")
    if len(positionals) >= 2:
        return RISK_ASK, "uniq 第二个文件参数会写文件"
    return RISK_ALLOW, ""


def _without_option_values(
    args: Sequence[str], value_flags: set[str],
) -> List[str]:
    result: List[str] = []
    skip = False
    for arg in args:
        if skip:
            skip = False
            continue
        if arg in value_flags:
            skip = True
            continue
        result.append(arg)
    return result


def _classify_git(args: Sequence[str]) -> Tuple[str, str]:
    if not args:
        return RISK_ASK, "git 缺少子命令"
    sub = args[0]
    rest = list(args[1:])
    if sub not in SAFE_GIT_SUBCOMMANDS:
        return RISK_ASK, f"git {sub} 有写副作用"
    if sub in GIT_OUTPUT_COMMANDS and (
        _has_any_option(rest, {"-o", "--output"})
        or any(arg.startswith("--output=") for arg in rest)
    ):
        return RISK_ASK, f"git {sub} 输出选项会写文件"
    if sub in GIT_OUTPUT_COMMANDS and _has_any_option(
            rest, {"--ext-diff", "--textconv"}):
        return RISK_ASK, f"git {sub} 会调用外部 diff/textconv 程序"
    if sub == "branch":
        return _classify_git_branch(rest)
    if sub == "tag":
        return _classify_git_tag(rest)
    if sub == "remote":
        return _classify_git_remote(rest)
    return RISK_ALLOW, ""


def _classify_git_branch(args: Sequence[str]) -> Tuple[str, str]:
    explicit_list = any(arg in {"-l", "--list"} for arg in args)
    remaining = _without_option_values(args, GIT_BRANCH_VALUE_FLAGS)
    for arg in remaining:
        if arg in GIT_BRANCH_LIST_FLAGS or any(
                arg.startswith(f"{flag}=") for flag in GIT_BRANCH_VALUE_FLAGS):
            continue
        if arg.startswith("-"):
            return RISK_ASK, f"git branch {arg} 可能修改分支"
        if not explicit_list:
            return RISK_ASK, "git branch <name> 会创建或修改分支"
    return RISK_ALLOW, ""


def _classify_git_tag(args: Sequence[str]) -> Tuple[str, str]:
    explicit_list = any(arg in {"-l", "--list"} for arg in args)
    remaining = _without_option_values(args, GIT_TAG_VALUE_FLAGS)
    for arg in remaining:
        if arg in GIT_TAG_LIST_FLAGS or arg.startswith("-n"):
            continue
        if any(arg.startswith(f"{flag}=") for flag in GIT_TAG_VALUE_FLAGS):
            continue
        if arg.startswith("-"):
            return RISK_ASK, f"git tag {arg} 可能修改标签"
        if not explicit_list:
            return RISK_ASK, "git tag <name> 会创建标签"
    return RISK_ALLOW, ""


def _classify_git_remote(args: Sequence[str]) -> Tuple[str, str]:
    if not args or all(arg in {"-v", "--verbose"} for arg in args):
        return RISK_ALLOW, ""
    action = args[0]
    if action in {"get-url"}:
        return RISK_ALLOW, ""
    if action == "show":
        return RISK_ASK, "git remote show 可能访问远端"
    return RISK_ASK, f"git remote {action} 会修改远端配置"
