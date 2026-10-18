import asyncio
import json
import sys
from dataclasses import replace
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest

from app.services import runtime as runtime_module
from app.services.events import EventHub
from app.services.runtime import DockerSandboxLauncher, RuntimeManager, RuntimeUnavailableError
from app.services.workspace import WorkspaceService
from test_runtime import FakeClock, FakeLauncher, finish_turn, jsonl, settings, wait_for_status


def test_protocol_close_waits_for_ack_and_exit_before_finishing(tmp_path: Path) -> None:
    async def scenario() -> None:
        config = settings(tmp_path)
        launcher = FakeLauncher()
        manager = RuntimeManager(config, WorkspaceService(config), EventHub(), launcher)
        await manager.activate("session")
        await wait_for_status(manager, "session", "idle")
        process = launcher.process
        process.stdin.on_close = None
        process.terminate = Mock(wraps=process.terminate)
        stop = asyncio.create_task(manager.stop_session("session"))
        for _ in range(50):
            if process.stdin.raw_writes:
                break
            await asyncio.sleep(0)
        assert json.loads(process.stdin.raw_writes[-1]) == {"version": 1, "type": "close"}
        assert not stop.done()
        await process.stdout.feed(jsonl({"type": "runtime_closed"}))
        for _ in range(20):
            await asyncio.sleep(0)
        assert manager._sessions["session"].closed.is_set()
        assert not stop.done()  # An acknowledgement alone is not process cleanup.
        process.exit()
        await asyncio.wait_for(stop, timeout=1)
        process.terminate.assert_not_called()
        assert manager.status("session") == "stopped"
        assert not manager._sessions["session"].cleanup_pending
        assert not any(e.type == "runtime_warning" for e in manager.events.history("session"))
        await manager.shutdown()

    asyncio.run(scenario())


@pytest.mark.parametrize("failure", ["no_ack", "ack_without_exit", "broken_stdin", "blocked_drain"])
def test_close_failure_falls_back_without_cleanup_false_positive(
    tmp_path: Path, monkeypatch, failure: str
) -> None:
    monkeypatch.setattr(runtime_module, "RUNTIME_CLOSE_TIMEOUT_SECONDS", 0.02)
    monkeypatch.setattr(runtime_module, "PROCESS_EXIT_TIMEOUT_SECONDS", 0.02)

    async def scenario() -> None:
        config = settings(tmp_path)
        launcher = FakeLauncher()
        manager = RuntimeManager(config, WorkspaceService(config), EventHub(), launcher)
        await manager.send_message("session", "task", turn_id="current")
        process = launcher.process
        process.stdin.on_close = None
        if failure == "ack_without_exit":
            process.stdin.on_close = lambda: process.stdout.queue.put_nowait(jsonl({"type": "runtime_closed"}))
        elif failure == "broken_stdin":
            process.stdin.drain = AsyncMock(side_effect=BrokenPipeError())
        elif failure == "blocked_drain":
            process.stdin.drain = lambda: asyncio.Event().wait()
        process.terminate = Mock()  # TERM does not stop this process.
        process.kill = Mock(side_effect=process.exit)
        await asyncio.wait_for(manager.stop_session("session"), timeout=1)
        process.terminate.assert_called_once()
        process.kill.assert_called_once()
        assert process.returncode == 0
        assert manager.status("session") == "stopped"
        assert not any(e.type == "runtime_warning" for e in manager.events.history("session"))
        await manager.shutdown()

    asyncio.run(scenario())


def test_process_exit_race_does_not_report_failed_cleanup(tmp_path: Path) -> None:
    async def scenario() -> None:
        config = settings(tmp_path)
        manager = RuntimeManager(config, WorkspaceService(config), EventHub(), FakeLauncher())
        from test_runtime import FakeProcess
        process = FakeProcess()

        def exit_race() -> None:
            process.exit()
            raise ProcessLookupError()

        process.terminate = exit_race
        assert await manager._terminate_process(process) == []

    asyncio.run(scenario())


def test_final_process_check_drops_errors_when_later_cleanup_reaps_process(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(runtime_module, "RUNTIME_CLOSE_TIMEOUT_SECONDS", 0.01)

    async def scenario() -> None:
        config = settings(tmp_path)
        launcher = FakeLauncher()
        manager = RuntimeManager(config, WorkspaceService(config), EventHub(), launcher,
                                 runtime_stop_hook=lambda *_args: exit_process())

        async def exit_process() -> None:
            launcher.process.exit()

        await manager.send_message("session", "task", turn_id="current")
        process = launcher.process
        process.stdin.on_close = None
        process.terminate = Mock(side_effect=ProcessLookupError())
        process.kill = Mock(side_effect=ProcessLookupError())
        process.wait = AsyncMock(side_effect=RuntimeError("wait failed"))
        await asyncio.wait_for(manager.stop_session("session"), timeout=1)
        assert process.returncode == 0
        assert manager.status("session") == "stopped"
        assert not manager.has_pending_cleanup("session")
        assert not any(e.type == "runtime_warning" for e in manager.events.history("session"))
        await manager.shutdown()

    asyncio.run(scenario())


@pytest.mark.parametrize("remove_code", [0, 1])
def test_container_cleanup_checks_final_state_even_when_remove_reports_error(
    tmp_path: Path, remove_code: int
) -> None:
    async def scenario() -> None:
        launcher = DockerSandboxLauncher(settings(tmp_path))
        launcher._run_docker = AsyncMock(side_effect=[
            (0, "owned-container\n", ""), (remove_code, "", "already removed"), (0, "", ""),
        ])
        await launcher.cleanup_session("session")
        calls = launcher._run_docker.await_args_list
        assert calls[1].args == ("rm", "-f", "owned-container")
        assert calls[0] == calls[2]
        assert "label=mycode-web.managed=true" in calls[0].args
        assert "label=mycode-web.session=session" in calls[0].args
        assert "name=^/mycode-web-session$" in calls[0].args

    asyncio.run(scenario())


@pytest.mark.parametrize("responses", [
    [(1, "", "daemon unavailable")],
    [(0, "owned", ""), (0, "", ""), (0, "owned", "")],
    [(0, "owned", ""), (0, "", ""), (1, "", "cannot verify")],
])
def test_container_cleanup_rejects_remaining_or_unverifiable_resources(
    tmp_path: Path, responses
) -> None:
    async def scenario() -> None:
        launcher = DockerSandboxLauncher(settings(tmp_path))
        launcher._run_docker = AsyncMock(side_effect=responses)
        with pytest.raises(RuntimeUnavailableError):
            await launcher.cleanup_session("session")

    asyncio.run(scenario())


def test_already_absent_container_does_not_run_remove(tmp_path: Path) -> None:
    async def scenario() -> None:
        launcher = DockerSandboxLauncher(settings(tmp_path))
        launcher._run_docker = AsyncMock(return_value=(0, "", ""))
        await launcher.cleanup_session("session")
        assert launcher._run_docker.await_count == 1
        assert launcher._run_docker.await_args.args[:2] == ("ps", "-aq")

    asyncio.run(scenario())


def test_container_cleanup_failure_retains_target_and_can_be_retried(tmp_path: Path) -> None:
    async def scenario() -> None:
        config = settings(tmp_path)
        launcher = FakeLauncher()
        launcher.container_ref = lambda session_id: f"container-{session_id}"
        launcher.cleanup_session = AsyncMock(side_effect=[
            RuntimeError("unavailable"), RuntimeError("unavailable"), RuntimeError("unavailable"), None,
        ])
        manager = RuntimeManager(config, WorkspaceService(config), EventHub(), launcher)
        await manager.activate("session")
        await wait_for_status(manager, "session", "idle")
        await manager.stop_session("session")
        state = manager._sessions["session"]
        assert manager.status("session") == "error"
        assert state.container_ref == "container-session"
        assert state.cleanup_pending
        assert manager.active_count == 1
        for action in (manager.activate("session"), manager.send_message("session", "retry")):
            with pytest.raises(RuntimeUnavailableError, match="cleanup is incomplete"):
                await action
        warning = next(e for e in manager.events.history("session") if e.type == "runtime_warning")
        assert warning.data["scope"] == "session"
        assert "turn_id" not in warning.data
        await manager.stop_session("session")
        assert manager.status("session") == "stopped"
        assert not state.cleanup_pending
        assert state.container_ref is None
        assert manager.active_count == 0
        assert await manager.activate("session") == "starting"
        await wait_for_status(manager, "session", "idle")
        assert not state.closed.is_set()  # Never reuse a previous generation's acknowledgement.
        launcher.cleanup_session.side_effect = None
        await manager.shutdown()

    asyncio.run(scenario())


@pytest.mark.parametrize("action", ["activate", "message"])
def test_recovered_cleanup_allows_original_action_without_deleting_session(tmp_path: Path, action: str) -> None:
    async def scenario() -> None:
        config = settings(tmp_path)
        launcher = FakeLauncher()
        launcher.container_ref = lambda session_id: f"container-{session_id}"
        launcher.cleanup_session = AsyncMock(side_effect=[RuntimeError("temporary"), None])
        manager = RuntimeManager(config, WorkspaceService(config), EventHub(), launcher)
        await manager.activate("session")
        await wait_for_status(manager, "session", "idle")
        await manager.stop_session("session")
        assert manager.has_pending_cleanup("session")
        if action == "activate":
            assert await manager.activate("session") == "starting"
            await wait_for_status(manager, "session", "idle")
        else:
            assert await manager.send_message("session", "new task", turn_id="new-turn") == "running"
            assert manager.active_turn_id("session") == "new-turn"
            messages = [e for e in manager.events.history("session") if e.type == "user_message"]
            assert len(messages) == 1
        assert not manager.has_pending_cleanup("session")
        assert manager.runtime_generation("session") == 2
        assert launcher.cleanup_session.await_count == 2
        launcher.cleanup_session.side_effect = None
        await manager.shutdown()

    asyncio.run(scenario())


@pytest.mark.parametrize("action", ["activate", "message"])
def test_failed_eviction_cannot_launch_another_runtime_over_capacity(tmp_path: Path, action: str) -> None:
    async def scenario() -> None:
        config = replace(settings(tmp_path), sandbox_max_active=1)
        launcher = FakeLauncher()
        launcher.container_ref = lambda session_id: f"container-{session_id}"
        launcher.cleanup_session = AsyncMock(side_effect=RuntimeError("container remains"))
        manager = RuntimeManager(config, WorkspaceService(config), EventHub(), launcher)
        await manager.send_message("victim", "task")
        await finish_turn(launcher.process)
        await wait_for_status(manager, "victim", "idle")
        if action == "activate":
            assert await manager.activate("target") == "starting"
            await wait_for_status(manager, "target", "error")
        else:
            with pytest.raises(RuntimeUnavailableError, match="unreclaimed Runtime"):
                await manager.send_message("target", "another task")
        assert launcher.calls == 1
        assert manager.has_pending_cleanup("victim")
        assert manager.status("target") == "error"
        assert manager.active_turn_id("target") is None
        launcher.cleanup_session.side_effect = None
        await manager.shutdown()

    asyncio.run(scenario())


def test_failed_ttl_cleanup_does_not_announce_successful_expiration(tmp_path: Path) -> None:
    async def scenario() -> None:
        config = settings(tmp_path)
        launcher = FakeLauncher()
        launcher.container_ref = lambda session_id: f"container-{session_id}"
        launcher.cleanup_session = AsyncMock(side_effect=RuntimeError("cannot verify removal"))
        clock = FakeClock()
        manager = RuntimeManager(config, WorkspaceService(config), EventHub(), launcher, clock=clock)
        await manager.activate("session")
        await wait_for_status(manager, "session", "idle")
        clock.advance(config.sandbox_idle_ttl_seconds + 1)
        assert await manager.sweep_expired() == ()
        assert manager.status("session") == "error"
        assert manager.has_pending_cleanup("session")
        assert not any(e.type == "runtime_expired" for e in manager.events.history("session"))
        launcher.cleanup_session.side_effect = None
        await manager.shutdown()

    asyncio.run(scenario())


@pytest.mark.parametrize("turn_id", [None, "", " ", 123, "old-turn"])
def test_invalid_finish_preserves_current_turn_permissions_and_published_state(
    tmp_path: Path, turn_id
) -> None:
    async def scenario() -> None:
        config = settings(tmp_path)
        launcher = FakeLauncher()
        manager = RuntimeManager(config, WorkspaceService(config), EventHub(), launcher)
        await manager.send_message("session", "task", turn_id="current")
        state = manager._sessions["session"]
        state.pending_permission = {"request_id": "permission"}
        state.status = "waiting_permission"
        before = manager.events.history("session")
        await manager._handle_message("session", state, {
            "type": "turn_finished", "status": "completed", "turn_id": turn_id,
        })
        assert manager.events.history("session") == before
        assert state.active_turn_id == "current"
        assert state.busy
        assert state.status == "waiting_permission"
        assert state.pending_permission == {"request_id": "permission"}
        await manager._handle_message("session", state, {
            "type": "turn_finished", "status": "completed", "turn_id": "current",
        })
        assert state.status == "idle"
        assert not state.busy
        assert state.active_turn_id is None
        assert state.pending_permission is None
        accepted = manager.events.history("session")
        await manager._handle_message("session", state, {"type": "turn_finished", "turn_id": "current"})
        assert manager.events.history("session") == accepted  # Duplicate completion is ignored.
        await manager.shutdown()

    asyncio.run(scenario())


def test_session_warning_does_not_inherit_active_turn(tmp_path: Path) -> None:
    async def scenario() -> None:
        config = settings(tmp_path)
        manager = RuntimeManager(config, WorkspaceService(config), EventHub(), FakeLauncher())
        await manager.send_message("session", "task", turn_id="current")
        for supplied, scope in [({}, "session"), ({"turn_id": "current"}, "turn")]:
            await manager._handle_message("session", manager._sessions["session"], {
                "type": "runtime_warning", "message": "warning", **supplied,
            })
            warning = manager.events.history("session")[-1]
            assert warning.data["scope"] == scope
            assert warning.data.get("turn_id") == supplied.get("turn_id")
        await manager.shutdown()

    asyncio.run(scenario())


def test_real_subprocess_protocol_close(tmp_path: Path) -> None:
    # Exercise real pipes, stdout framing, process exit and finalizer concurrency.
    class Launcher:
        async def launch(self, *_args):
            return await asyncio.create_subprocess_exec(
                sys.executable, "-u", "-c",
                'import json,sys\n'
                'print(json.dumps({"version":1,"type":"runtime_ready"}),flush=True)\n'
                'for line in sys.stdin:\n'
                ' if json.loads(line)["type"] == "close":\n'
                '  print(json.dumps({"version":1,"type":"runtime_closed"}),flush=True)\n'
                '  break\n',
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )

    async def scenario() -> None:
        config = settings(tmp_path)
        manager = RuntimeManager(config, WorkspaceService(config), EventHub(), Launcher())
        await manager.activate("session")
        await asyncio.wait_for(manager.wait_until_ready("session"), timeout=5)
        process = manager._sessions["session"].process
        process.terminate = Mock(wraps=process.terminate)
        try:
            await asyncio.wait_for(manager.stop_session("session"), timeout=5)
            assert process.returncode == 0
            process.terminate.assert_not_called()
            assert manager._sessions["session"].closed.is_set()
            assert manager.status("session") == "stopped"
        finally:
            await manager.shutdown()

    asyncio.run(scenario())
