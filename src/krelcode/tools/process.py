"""子进程树终止的跨平台工具。

前台 run_command 与后台 ShellJobManager 共用同一实现，避免两套进程组
清理逻辑漂移。POSIX 优先终止整个 session/process group；Windows 退化为
终止主进程。
"""

from __future__ import annotations

import asyncio
import os
import signal
from typing import Optional


def terminate_process_tree(
    proc: asyncio.subprocess.Process,
    *,
    sig: Optional[int] = None,
) -> bool:
    """终止子进程树；返回是否成功向预期目标发送了信号。"""
    if os.name == "posix":
        target_signal = signal.SIGKILL if sig is None else sig
        try:
            # start_new_session=True 使 pgid == 主进程 pid。主进程已退出但
            # 后代仍存活时 getpgid(pid) 会失败，直接按已知 pgid 发信号。
            try:
                pgid = os.getpgid(proc.pid)
            except ProcessLookupError:
                pgid = proc.pid
            os.killpg(pgid, target_signal)
            return True
        except (ProcessLookupError, PermissionError, OSError, AttributeError):
            pass
    try:
        proc.kill()
        return True
    except (ProcessLookupError, OSError):
        return False
