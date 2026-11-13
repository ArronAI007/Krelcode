"""子进程环境净化：默认不把宿主秘密传给工具进程。"""

from __future__ import annotations

import fnmatch
from collections.abc import Iterable, Mapping, Sequence
from typing import Dict

SENSITIVE_ENV_PATTERNS: Sequence[str] = (
    "*_API_KEY", "*_API_TOKEN", "*_TOKEN", "*_SECRET", "*_SECRETS",
    "*_PASSWORD", "*_PASSWD", "*_CREDENTIAL", "*_CREDENTIALS",
    "*_PRIVATE_KEY", "*_ACCESS_KEY", "*_SESSION_TOKEN", "*_PROXY",
)

SENSITIVE_ENV_NAMES: frozenset[str] = frozenset({
    "AWS_ACCESS_KEY_ID",
    "GOOGLE_APPLICATION_CREDENTIALS",
    "SSH_AUTH_SOCK",
    "GPG_AGENT_INFO",
    "SSH_ASKPASS",
    "GIT_ASKPASS",
    "SUDO_ASKPASS",
    "KUBECONFIG",
    "DOCKER_CONFIG",
    "GIT_CONFIG_GLOBAL",
    "NETRC",
    "BASH_ENV",
    "ENV",
    "SHELLOPTS",
    "CDPATH",
    "LD_PRELOAD",
    "DYLD_INSERT_LIBRARIES",
    "PYTHONSTARTUP",
    "PYTHONINSPECT",
    "NODE_OPTIONS",
    "PERL5OPT",
    "RUBYOPT",
    "SSLKEYLOGFILE",
})


def scrub_environment(
    env: Mapping[str, str],
    extra_names: Iterable[str] = (),
    patterns: Sequence[str] = SENSITIVE_ENV_PATTERNS,
    names: frozenset[str] = SENSITIVE_ENV_NAMES,
) -> Dict[str, str]:
    """返回移除密钥、认证代理和 shell 注入变量后的环境副本。"""
    scrubbed: Dict[str, str] = {}
    blocked_names = {name.upper() for name in extra_names}
    for key, value in env.items():
        upper_key = key.upper()
        if upper_key in names or upper_key in blocked_names:
            continue
        if any(fnmatch.fnmatch(upper_key, pattern) for pattern in patterns):
            continue
        scrubbed[key] = value
    return scrubbed
