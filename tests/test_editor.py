"""编辑器纯逻辑测试：文本缓冲、历史、搜索与前台队列。"""

import json
import stat

from krelcode.ui.editor import (
    EditBuffer,
    HistorySearch,
    InputHistory,
    sanitize_pasted_text,
)
from krelcode.ui.queue import PromptQueue


def test_edit_buffer_cursor_motion_and_deletion():
    buffer = EditBuffer("alpha beta\ngamma delta", cursor=5)
    assert buffer.move_left() is True and buffer.cursor == 4
    assert buffer.move_right() is True and buffer.cursor == 5
    assert buffer.move_home() is True and buffer.cursor == 0
    assert buffer.move_end() is True and buffer.cursor == 10
    assert buffer.delete() is True
    assert buffer.text == "alpha betagamma delta"

    buffer.set_text("one two three", cursor=len("one two three"))
    assert buffer.move_word_left() is True
    assert buffer.cursor == len("one two ")
    assert buffer.delete_word_before() is True
    assert buffer.text == "one three"
    assert buffer.delete_to_start() is True
    assert buffer.text == "three" and buffer.cursor == 0


def test_edit_buffer_multiline_vertical_motion_and_indexes():
    buffer = EditBuffer("第一行\nabcdef\n尾行", cursor=2)
    assert buffer.cursor_line_column() == (0, 2)
    assert buffer.move_line_down() is True
    assert buffer.cursor_line_column() == (1, 2)
    assert buffer.move_line_down() is True
    assert buffer.cursor_line_column() == (2, 2)
    assert buffer.move_line_up() is True
    assert buffer.cursor_line_column() == (1, 2)

    buffer.set_text("abc\ndef", cursor=len(buffer.text))
    buffer.insert("\nghi")
    assert buffer.text == "abc\ndef\nghi"


def test_sanitize_paste_normalizes_newlines_and_removes_controls():
    pasted = "第一行\r\n\tcode\x00\x1b[31m\rred\x1b[0m"
    cleaned = sanitize_pasted_text(pasted)
    assert cleaned == "第一行\n    code[31m\nred[0m"
    assert "\x1b" not in cleaned and "\x00" not in cleaned


def test_history_jsonl_roundtrip_dedup_and_redaction(tmp_path):
    path = tmp_path / "history.jsonl"
    secret = "sk-super-secret-value"
    history = InputHistory(path=path, redact=lambda text: text.replace(secret, "***"))
    history.add("第一个问题")
    history.add("第一个问题")
    history.add("/help")
    history.add(f"使用 {secret} 调试")

    assert history.entries == ("第一个问题", "使用 *** 调试")
    assert secret not in path.read_text(encoding="utf-8")
    assert stat.S_IMODE(path.stat().st_mode) == 0o600

    restored = InputHistory(path=path)
    assert restored.entries == history.entries


def test_history_load_skips_commands_and_tolerates_corrupt_lines(tmp_path):
    path = tmp_path / "history.jsonl"
    path.write_text(
        "\n".join((
            "{broken json",
            json.dumps({"text": "/exit"}, ensure_ascii=False),
            json.dumps({"text": "有效问题"}, ensure_ascii=False),
        )) + "\n",
        encoding="utf-8",
    )
    history = InputHistory(path=path)
    assert history.entries == ("{broken json", "有效问题")


def test_history_search_finds_queries_cycles_and_restores_draft():
    history = InputHistory()
    history.add("fix parser bug")
    history.add("explain renderer")
    history.add("fix terminal cursor")
    buffer = EditBuffer("未提交草稿", cursor=3)
    search = HistorySearch.begin(buffer)

    search.query = "fix"
    assert search.find(history) is True
    assert search.match_text == "fix terminal cursor"
    assert search.cycle(history) is True
    assert search.match_text == "fix parser bug"
    search.cancel(buffer)
    assert buffer.text == "未提交草稿" and buffer.cursor == 3


def test_prompt_queue_supports_management_operations():
    queue = PromptQueue[str]()
    assert queue.append("a") == 1
    queue.append("b")
    queue.append("c")
    assert queue.delete(2) == "b"
    queue.move(2, 1)
    assert queue.entries == ("c", "a")
    queue.clear()
    assert not queue and len(queue) == 0


def test_prompt_queue_invalid_positions_are_rejected():
    queue = PromptQueue[str]()
    queue.append("only")
    for operation in (
        lambda: queue.delete(0),
        lambda: queue.delete(2),
        lambda: queue.move(1, 2),
    ):
        try:
            operation()
        except IndexError:
            pass
        else:  # pragma: no cover - 明确失败而不是静默改错位置
            raise AssertionError("非法队列序号必须报错")
