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
from typing import Dict, List, Optional, Sequence, Tuple

RISK_ALLOW = "allow"
RISK_ASK = "ask"
RISK_DENY = "deny"

_RISK_ORDER = {RISK_ALLOW: 0, RISK_ASK: 1, RISK_DENY: 2}

SEPARATORS = {"&&", "||", ";", "|", "&", "(", ")"}
REDIRECTS = {">", ">>", "<"}

# 只读命令白名单（无副作用）
SAFE_PROGRAMS = {
    "ls", "cat", "pwd", "echo", "head", "tail", "wc", "find", "grep", "rg",
    "which", "whoami", "date", "env", "printenv", "tree", "file", "stat",
    "du", "df", "ps", "uname", "basename", "dirname", "sort", "uniq", "cut",
    "tr", "diff", "md5", "shasum", "true", "false", "id", "hostname",
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

    # 3) 切段
    segments: List[List[str]] = []
    current: List[str] = []
    has_redirect = False
    for tok in tokens:
        if tok in SEPARATORS:
            if current:
                segments.append(current)
                current = []
        elif tok in REDIRECTS:
            has_redirect = True
            if current:
                segments.append(current)
                current = []
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
    lex = shlex.shlex(raw, punctuation_chars=True)
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
    if prog == "git":
        sub = args[0] if args else ""
        if sub in SAFE_GIT_SUBCOMMANDS:
            return RISK_ALLOW, ""
        return RISK_ASK, f"git {sub or '(无子命令)'} 有写副作用"
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
