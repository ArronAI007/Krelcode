"""REPL 显式会话状态机测试。"""

import pytest

from krelcode.session.state import SessionPhase, SessionViewState


def test_new_state_is_idle():
    state = SessionViewState()
    assert state.phase is SessionPhase.IDLE
    assert state.is_running is False
    assert state.is_stopping is False
    assert state.current_turn_id is None
    assert state.progress == ""


def test_turn_lifecycle_generates_stable_ids_and_resets_progress():
    state = SessionViewState()

    first = state.begin_turn()
    assert first == "turn-1"
    assert state.phase is SessionPhase.RUNNING
    state.update_progress("执行工具")
    assert state.progress == "执行工具"
    state.finish_turn()

    second = state.begin_turn("压缩上下文")
    assert second == "turn-2"
    assert state.progress == "压缩上下文"
    state.finish_turn()
    assert state.current_turn_id is None
    assert state.progress == ""


def test_running_state_rejects_a_second_turn():
    state = SessionViewState()
    state.begin_turn()
    with pytest.raises(RuntimeError, match="已有任务"):
        state.begin_turn()


def test_stop_transition_and_idle_cleanup_are_idempotent():
    state = SessionViewState()
    assert state.request_stop() is False

    state.begin_turn()
    assert state.request_stop() is True
    assert state.phase is SessionPhase.STOPPING
    assert state.progress == "正在中止"

    state.finish_turn()
    state.finish_turn()
    assert state.phase is SessionPhase.IDLE


def test_progress_is_ignored_while_idle():
    state = SessionViewState()
    state.update_progress("不应保留")
    assert state.progress == ""
