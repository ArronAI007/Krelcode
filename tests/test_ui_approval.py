"""ui/approval 测试：输入解析（纯函数）。"""

from krelcode.ui.approval import ALWAYS, NO, YES, parse_approval_input


def test_parse_yes_variants():
    assert parse_approval_input("y") == YES
    assert parse_approval_input("Y") == YES
    assert parse_approval_input(" yes ") == YES


def test_parse_always_variants():
    assert parse_approval_input("a") == ALWAYS
    assert parse_approval_input("Always") == ALWAYS


def test_parse_no_and_defaults():
    assert parse_approval_input("n") == NO
    assert parse_approval_input("") == NO          # 空输入按 no（保守）
    assert parse_approval_input("maybe") == NO     # 无法识别按 no
    assert parse_approval_input(None) == NO
