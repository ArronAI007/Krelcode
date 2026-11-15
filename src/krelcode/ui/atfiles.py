"""兼容导入：@文件/图片展开已下沉到会话输入层。

保留本模块是为了兼容已有嵌入方；新代码应使用
:mod:`krelcode.session.attachments`。
"""

from __future__ import annotations

from pathlib import Path
from typing import List, Tuple

from ..core.messages import ImageRef
from ..session import attachments as _attachments

MAX_FILE_CHARS = _attachments.MAX_FILE_CHARS
MAX_FILES = _attachments.MAX_FILES
MAX_FILE_BYTES = _attachments.MAX_FILE_BYTES
IMAGE_EXTS = _attachments.IMAGE_EXTS
MEDIA_TYPES = _attachments.MEDIA_TYPES
MAX_IMAGE_BYTES = _attachments.MAX_IMAGE_BYTES
MAX_IMAGES = _attachments.MAX_IMAGES
AT_PATTERN = _attachments.AT_PATTERN
_resolve_in_workspace = _attachments._resolve_in_workspace


def expand_at_files(
    text: str,
    workspace_root: Path,
    max_file_chars: int = MAX_FILE_CHARS,
    max_files: int = MAX_FILES,
) -> Tuple[str, List[str], List[ImageRef]]:
    return _attachments.expand_at_files(
        text,
        workspace_root,
        max_file_chars=max_file_chars,
        max_files=max_files,
        max_image_bytes=MAX_IMAGE_BYTES,
        max_images=MAX_IMAGES,
    )
