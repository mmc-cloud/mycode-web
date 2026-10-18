import asyncio
from dataclasses import replace
from pathlib import Path

import pytest

from app.services.events import EventHub
from app.services.runtime import RuntimeManager
from app.services.workspace import WorkspaceService
from test_runtime import FakeLauncher, finish_turn, settings, wait_for_status


class CleanupGateLauncher(FakeLauncher):
    """Track containers independently of their attached client processes."""

    def __init__(self, *blocked_sessions: str) -> None:
        super().__init__()
        self.gates = {key: (asyncio.Event(), asyncio.Event()) for key in blocked_sessions}
        self.containers: set[str] = set()
        self.max_containers = 0

    def container_ref(self, session_id: str) -> str:
        return f"container-{session_id}"

    async def launch(self, session_id, workspace, mycode_state):
        process = await super().launch(session_id, workspace, mycode_state)
        self.containers.add(session_id)
        self.max_containers = max(self.max_containers, len(self.containers))
        return process

    async def cleanup_session(self, session_id: str) -> None:
        if session_id in self.gates:
            entered, release = self.gates[session_id]
            entered.set()
            await release.wait()
        self.containers.discard(session_id)

    def release_all(self) -> None:
        for _, release in self.gates.values():
            release.set()


@pytest.mark.parametrize("phase", ["process_alive", "container_cleanup"])
@pytest.mark.parametrize("quota", ["global", "user"])
def test_stopping_holds_capacity_until_process_and_container_cleanup(
    tmp_path: Path, phase: str, quota: str
) -> None:
    async def scenario() -> None:
        config = replace(settings(tmp_path), sandbox_max_active=1 if quota == "global" else 2,
                         sandbox_max_active_per_user=1)
        launcher = CleanupGateLauncher(*(["a"] if phase == "container_cleanup" else []))
        manager = RuntimeManager(
            config, WorkspaceService(config), EventHub(), launcher,
            session_owner_resolver=(lambda _key: "owner") if quota == "user" else None,
        )
        await manager.activate("a")
        await wait_for_status(manager, "a", "idle")
        process = launcher.process
        if phase == "process_alive":
            process.stdin.on_close = None
        stop = asyncio.create_task(manager.stop_session("a"))
        try:
            if phase == "process_alive":
                await wait_for_status(manager, "a", "stopping")
                assert process.returncode is None
            else:
                await asyncio.wait_for(launcher.gates["a"][0].wait(), timeout=1)
                assert process.returncode == 0
            assert manager._occupied_slots_locked() == 1
            assert manager.active_count == 1
            assert await manager.activate("b") == "stopped"
            assert await manager.send_message("b", "queued task", turn_id="b-turn") == "queued"
            assert launcher.calls == 1
            assert manager.status("b") == "queued"
            process.close() if phase == "process_alive" else launcher.release_all()
            await asyncio.wait_for(stop, timeout=1)
            await wait_for_status(manager, "b", "running")
            assert manager.active_turn_id("b") == "b-turn"
            assert launcher.containers == {"b"}
            assert launcher.max_containers == 1
        finally:
            launcher.release_all()
            if process.returncode is None:
                process.close()
            await manager.shutdown()

    asyncio.run(scenario())


@pytest.mark.parametrize("per_user", [False, True])
def test_two_concurrent_queue_handoffs_share_reserved_slots_without_starvation(
    tmp_path: Path, per_user: bool
) -> None:
    async def scenario() -> None:
        config = replace(settings(tmp_path), sandbox_max_active=2, sandbox_max_active_per_user=2)
        launcher = CleanupGateLauncher("a", "c")
        manager = RuntimeManager(
            config, WorkspaceService(config), EventHub(), launcher,
            session_owner_resolver=(lambda _key: "owner") if per_user else None,
        )
        try:
            await manager.send_message("a", "first")
            await manager.send_message("c", "second")
            assert await manager.send_message("b", "third") == "queued"
            assert await manager.send_message("d", "fourth") == "queued"
            await finish_turn(launcher.by_session["a"][0])
            await asyncio.wait_for(launcher.gates["a"][0].wait(), timeout=1)
            await finish_turn(launcher.by_session["c"][0])
            await asyncio.wait_for(launcher.gates["c"][0].wait(), timeout=1)
            assert manager.status("b") == manager.status("d") == "starting"
            assert manager._occupied_slots_locked() == 2
            assert launcher.calls == 2
            assert await manager.send_message("e", "fifth") == "queued"
            launcher.gates["a"][1].set()
            await wait_for_status(manager, "b", "running")
            assert manager.status("d") == "starting"
            assert manager.status("e") == "queued"
            assert launcher.containers == {"b", "c"}
            assert manager._occupied_slots_locked() == 2
            launcher.gates["c"][1].set()
            await wait_for_status(manager, "d", "running")
            assert launcher.containers == {"b", "d"}
            assert manager.status("e") == "queued"
            await manager.stop_session("b")
            await wait_for_status(manager, "e", "running")
            assert launcher.max_containers <= 2
            assert manager.queued_count == 0
        finally:
            launcher.release_all()
            await manager.shutdown()

    asyncio.run(scenario())


def test_cross_user_handoff_reserves_new_owner_quota_while_old_owner_still_holds_slot(tmp_path: Path) -> None:
    async def scenario() -> None:
        config = replace(settings(tmp_path), sandbox_max_active=2, sandbox_max_active_per_user=1)
        owners = {"a": "old", "a2": "old", "b": "new", "b2": "new", "busy": "other"}
        launcher = CleanupGateLauncher("a")
        manager = RuntimeManager(config, WorkspaceService(config), EventHub(), launcher,
                                 session_owner_resolver=owners.get)
        try:
            await manager.activate("a")
            await wait_for_status(manager, "a", "idle")
            await manager.send_message("busy", "keep busy")
            assert await manager.activate("b") == "starting"
            await asyncio.wait_for(launcher.gates["a"][0].wait(), timeout=1)
            assert manager._occupied_slots_locked() == 2
            assert not manager._user_has_capacity_locked("a2")
            assert not manager._user_has_capacity_locked("b2")
            assert await manager.activate("a2") == "stopped"
            assert await manager.activate("b2") == "stopped"
            assert launcher.calls == 2
            launcher.release_all()
            await wait_for_status(manager, "b", "idle")
            assert manager._user_has_capacity_locked("a2")
            assert not manager._user_has_capacity_locked("b2")
            assert launcher.max_containers <= 2
        finally:
            launcher.release_all()
            await manager.shutdown()

    asyncio.run(scenario())


@pytest.mark.parametrize("reuse_target", [False, True])
def test_cancelled_handoff_target_does_not_launch_or_release_source_capacity_early(
    tmp_path: Path, reuse_target: bool
) -> None:
    async def scenario() -> None:
        config = replace(settings(tmp_path), sandbox_max_active=1)
        launcher = CleanupGateLauncher("a")
        manager = RuntimeManager(config, WorkspaceService(config), EventHub(), launcher)
        queued_start = asyncio.Event()
        original_start = manager._start_queued_turn

        async def delayed_start(item, state, generation):
            await queued_start.wait()
            await original_start(item, state, generation)

        manager._start_queued_turn = delayed_start
        try:
            await manager.send_message("a", "first")
            assert await manager.send_message("b", "cancel this") == "queued"
            await finish_turn(launcher.by_session["a"][0])
            await asyncio.wait_for(launcher.gates["a"][0].wait(), timeout=1)
            assert manager.status("b") == "starting"
            await manager.stop_session("b")
            assert manager.status("b") == "stopped"
            assert manager._occupied_slots_locked() == 1
            next_session = "b" if reuse_target else "c"
            assert await manager.send_message(next_session, "next task", turn_id="next-turn") == "queued"
            assert launcher.calls == 1
            launcher.release_all()
            await wait_for_status(manager, "a", "stopped")
            for _ in range(20):
                await asyncio.sleep(0)
            if reuse_target:
                assert not launcher.by_session.get("b")
            queued_start.set()
            await wait_for_status(manager, next_session, "running")
            assert len(launcher.by_session[next_session]) == 1
            assert launcher.by_session[next_session][0].stdin.writes == [b"next task\n"]
            if not reuse_target:
                assert "b" not in launcher.by_session
            assert launcher.max_containers == 1
        finally:
            queued_start.set()
            launcher.release_all()
            await manager.shutdown()

    asyncio.run(scenario())
