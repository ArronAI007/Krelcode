"""@文件引用：把用户输入里的 @path 展开为随消息注入的文件内容块。

主流 coding agent 的标配 UX（Claude Code / Cursor 同款）。
在 UI 层展开（进入 history 的已是展开后文本），provider/内核零感知。

规则：
- 词首 @（前面是行首/空白）才触发，email 等不误伤
- 路径解析与工具层同规则：相对工作区、resolve 后必须仍在工作区内
- 不存在/二进制/越界/超限 → 保留原文并在 notes 里说明（fail-safe）
- 单文件与单条消息都有预算上限，内容截断有标记
- **图片**（png/jpeg/webp/gif）：不注入文本，作为 ImageRef 附件随消息
  传给 provider（多模态），token 位置留 "[图片 xxx 已附件]" 占位
"""

from __future__ import annotations

import base64
import re
from pathlib import Path
from typing import List, Tuple

from ..core.messages import ImageRef

AT_PATTERN = re.compile(r"(?<![\w@])@([A-Za-z0-9_./\\-]+)")

MAX_FILE_CHARS = 8_000     # 单文件注入上限
MAX_FILES = 5              # 单条消息注入文件数上限
MAX_FILE_BYTES = 1_000_000  # 超过 1MB 不读

IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".gif"}
MEDIA_TYPES = {
    ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
    ".webp": "image/webp", ".gif": "image/gif",
}
MAX_IMAGE_BYTES = 5_000_000   # Anthropic 上限约 5MB，取各家最严
MAX_IMAGES = 4                # 单条消息图片数上限


def expand_at_files(
    text: str,
    workspace_root: Path,
    max_file_chars: int = MAX_FILE_CHARS,
    max_files: int = MAX_FILES,
) -> Tuple[str, List[str], List[ImageRef]]:
    """返回 (展开后文本, 提示列表, 图片附件列表)。

    文本文件 → <attached_file> 块注入；图片 → ImageRef 附件（占位文本）。
    """
    root = Path(workspace_root).resolve()
    notes: List[str] = []
    images: List[ImageRef] = []
    injected = 0

    def replace(match: "re.Match[str]") -> str:
        nonlocal injected
        raw = match.group(1)
        token = match.group(0)
        target = _resolve_in_workspace(raw, root)
        suffix = Path(raw).suffix.lower()

        # 图片分支：作为多模态附件，不占文本注入配额
        if suffix in IMAGE_EXTS:
            if len(images) >= MAX_IMAGES:
                notes.append(f"@{raw}: 超出单条消息图片上限（{MAX_IMAGES} 张），保留原文")
                return token
            if target is None:
                notes.append(f"@{raw}: 路径越出工作区或非法，保留原文")
                return token
            if not target.is_file():
                notes.append(f"@{raw}: 图片不存在，保留原文")
                return token
            try:
                size = target.stat().st_size
                if size > MAX_IMAGE_BYTES:
                    notes.append(f"@{raw}: 图片过大（{size} > {MAX_IMAGE_BYTES} 字节），保留原文")
                    return token
                data = target.read_bytes()
            except OSError as e:
                notes.append(f"@{raw}: 图片不可读（{e}），保留原文")
                return token
            try:
                rel = target.relative_to(root).as_posix()
            except ValueError:
                rel = target.name
            images.append(ImageRef(
                media_type=MEDIA_TYPES[suffix],
                data_b64=base64.b64encode(data).decode("ascii"),
                source_path=str(target),
            ))
            notes.append(f"@{raw}: 已附加图片 {rel}（{size} 字节）")
            return f"[图片 {rel} 已附件]"

        # 文本分支（原逻辑）
        if injected >= max_files:
            notes.append(f"@{raw}: 超出单条消息注入上限（{max_files} 个），保留原文")
            return token
        if target is None:
            notes.append(f"@{raw}: 路径越出工作区或非法，保留原文")
            return token
        if not target.is_file():
            notes.append(f"@{raw}: 文件不存在，保留原文")
            return token
        try:
            if target.stat().st_size > MAX_FILE_BYTES:
                notes.append(f"@{raw}: 文件过大（>1MB），保留原文")
                return token
            content = target.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            notes.append(f"@{raw}: 非 UTF-8 文本或不可读，保留原文")
            return token
        truncated = ""
        if len(content) > max_file_chars:
            content = content[:max_file_chars]
            truncated = f"\n…[截断，仅注入前 {max_file_chars} 字符]"
        try:
            rel = target.relative_to(root).as_posix()
        except ValueError:
            rel = target.name
        injected += 1
        notes.append(f"@{raw}: 已注入 {rel}（{len(content)} 字符）")
        return f'\n<attached_file path="{rel}">\n{content}{truncated}\n</attached_file>\n'

    expanded = AT_PATTERN.sub(replace, text)
    return expanded, notes, images


def _resolve_in_workspace(raw: str, root: Path):
    try:
        candidate = Path(raw).expanduser()
        if not candidate.is_absolute():
            candidate = root / candidate
        resolved = candidate.resolve()
    except (OSError, ValueError):
        return None
    if resolved != root and root not in resolved.parents:
        return None
    return resolved
