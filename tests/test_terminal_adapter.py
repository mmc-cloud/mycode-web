from app.services.terminal_adapter import TerminalOutputAdapter


def test_terminal_adapter_handles_split_prompts_and_permission_fields() -> None:
    adapter = TerminalOutputAdapter()
    assert adapter.feed("boot\nyo") == []
    ready = adapter.feed("u> ")
    assert ready[0].type == "ready"

    assert adapter.feed("permission> run_command 需要确认\n") == []
    assert adapter.feed("target> pytest\nreason> command asks\n是否批") == []
    request = adapter.feed(
        "准？[y/yes 本次 | t/task 当前任务 | s/session 当前会话 | N 拒绝] "
    )
    assert request[0].type == "permission_request"
    assert request[0].data == {
        "summary": "run_command 需要确认",
        "target": "pytest",
        "reason": "command asks",
    }
    assert adapter.awaiting_permission is True
    adapter.resolve_permission()
    assert adapter.awaiting_permission is False
