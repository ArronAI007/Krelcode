"""Step 37 测试：多模态图片输入（@附件 → ImageRef → 三家 wire → 会话持久化）。"""

import asyncio
import base64
import json
from pathlib import Path

import pytest

import krelcode.ui.atfiles as atfiles
from krelcode.agent.loop import AgentLoop
from krelcode.core.messages import ImageRef, Message, ToolCall
from krelcode.llm import collect
from krelcode.llm.providers.anthropic import AnthropicProvider, to_anthropic_request_messages
from krelcode.llm.providers.gemini import GeminiProvider, to_gemini_contents
from krelcode.llm.providers.mock import MockProvider
from krelcode.llm.providers.openai import OpenAIProvider, to_openai_messages
from krelcode.safety.policy import AllowAllPolicy
from krelcode.session.codec import history_from_dicts, history_to_dicts
from krelcode.session.export import render_markdown
from krelcode.tools.base import ToolContext
from krelcode.tools.registry import ToolRegistry
from krelcode.ui.atfiles import expand_at_files

PNG_1PX = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmM"
    "IQAAAABJRU5ErkJggg==")


@pytest.fixture
def ws(tmp_path):
    (tmp_path / "shot.png").write_bytes(PNG_1PX)
    (tmp_path / "photo.jpg").write_bytes(b"\xff\xd8\xff\xe0fakejpeg")
    (tmp_path / "notes.txt").write_text("文本", encoding="utf-8")
    return tmp_path


# -- @图片附件（atfiles）--------------------------------------------------------------------------

def test_image_attached_as_imageref(ws):
    text, notes, images = expand_at_files("看这个报错 @shot.png", ws)
    assert len(images) == 1
    img = images[0]
    assert img.media_type == "image/png"
    assert img.data_b64 == base64.b64encode(PNG_1PX).decode("ascii")
    assert img.source_path == str((ws / "shot.png").resolve())
    # 文本里是占位符而非 base64
    assert "[图片 shot.png 已附件]" in text and img.data_b64 not in text
    assert any("已附加图片" in n for n in notes)


def test_image_and_text_mixed(ws):
    text, notes, images = expand_at_files("@notes.txt 和 @photo.jpg", ws)
    assert len(images) == 1 and images[0].media_type == "image/jpeg"
    assert '<attached_file path="notes.txt">' in text      # 文本照常注入
    assert "[图片 photo.jpg 已附件]" in text


def test_image_oversize_rejected(ws, monkeypatch):
    monkeypatch.setattr(atfiles, "MAX_IMAGE_BYTES", 10)
    text, notes, images = expand_at_files("@shot.png", ws)
    assert images == [] and "@shot.png" in text
    assert any("图片过大" in n for n in notes)


def test_image_count_cap(ws, monkeypatch):
    monkeypatch.setattr(atfiles, "MAX_IMAGES", 1)
    text, notes, images = expand_at_files("@shot.png @photo.jpg", ws)
    assert len(images) == 1
    assert "@photo.jpg" in text                       # 超限保留原文
    assert any("图片上限" in n for n in notes)


def test_image_missing_or_outside(ws):
    text, notes, images = expand_at_files("@ghost.png @../outside.png", ws)
    assert images == []
    assert any("图片不存在" in n for n in notes)
    assert any("越出工作区" in n for n in notes)


# -- 三家 provider wire 转换 -------------------------------------------------------------------------

def sample_msg():
    return Message.user("看这个报错", images=[
        ImageRef("image/png", base64.b64encode(PNG_1PX).decode("ascii"), "/x/shot.png")])


def test_openai_image_wire():
    wire = to_openai_messages([sample_msg()])[0]
    blocks = wire["content"]
    assert blocks[0] == {"type": "text", "text": "看这个报错"}
    assert blocks[1]["type"] == "image_url"
    assert blocks[1]["image_url"]["url"].startswith("data:image/png;base64,")


def test_anthropic_image_wire():
    _, wire = to_anthropic_request_messages([sample_msg()])
    blocks = wire[0]["content"]
    assert blocks[0] == {"type": "text", "text": "看这个报错"}
    assert blocks[1]["type"] == "image"
    assert blocks[1]["source"] == {"type": "base64", "media_type": "image/png",
                                   "data": base64.b64encode(PNG_1PX).decode("ascii")}


def test_gemini_image_wire():
    _, contents = to_gemini_contents([sample_msg()])
    parts = contents[0]["parts"]
    assert parts[0] == {"text": "看这个报错"}
    assert parts[1]["inline_data"]["mime_type"] == "image/png"


def test_images_do_not_leak_into_non_user_roles():
    """assistant/tool 消息即使误挂 images 也不产生多模态块（防御性）。"""
    m = Message.assistant("x")
    m.images = [ImageRef("image/png", "QQ==", None)]
    wire = to_openai_messages([m])[0]
    assert wire["content"] == "x"


# -- loop 与持久化 ------------------------------------------------------------------------------------

def test_loop_run_attaches_images(tmp_path):
    loop = AgentLoop(
        llm=MockProvider(scripted=["看到了"]),
        registry=ToolRegistry(),
        tool_context=ToolContext(workspace_root=tmp_path),
        policy=AllowAllPolicy(), system_prompt="SYS",
    )
    img = ImageRef("image/png", "QQ==", str(tmp_path / "a.png"))
    reply = asyncio.run(loop.run("看图", images=[img]))
    assert reply == "看到了"
    assert loop.history[1].images == [img]


def test_codec_persists_by_path_and_reloads(ws):
    img = ImageRef("image/png", base64.b64encode(PNG_1PX).decode("ascii"),
                   str(ws / "shot.png"))
    history = [Message.user("看图", images=[img]), Message.assistant("看到了")]
    dicts = history_to_dicts(history)
    # 序列化只存路径，不存 base64（防 JSONL 膨胀）
    assert dicts[0]["images"] == [{"media_type": "image/png",
                                   "source_path": str(ws / "shot.png")}]
    assert "data_b64" not in json.dumps(dicts[0])

    restored = history_from_dicts(dicts)
    assert restored[0].images is not None
    assert restored[0].images[0].data_b64 == img.data_b64     # 按路径重载成功
    assert restored[0].images[0].media_type == "image/png"


def test_codec_drops_image_when_source_deleted(tmp_path):
    src = tmp_path / "gone.png"
    src.write_bytes(PNG_1PX)
    img = ImageRef("image/png", base64.b64encode(PNG_1PX).decode("ascii"), str(src))
    dicts = history_to_dicts([Message.user("看图", images=[img])])
    src.unlink()                                               # 源文件被删
    restored = history_from_dicts(dicts)
    assert restored[0].images is None                          # 静默丢弃，不报错


def test_export_marks_image_placeholder(ws):
    img = ImageRef("image/png", "QQ==", str(ws / "shot.png"))
    md = render_markdown([Message.user("看图", images=[img])])
    assert "🖼" in md and str(ws / "shot.png") in md
    assert "QQ==" not in md                                    # base64 不进导出
