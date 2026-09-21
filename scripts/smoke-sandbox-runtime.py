#!/usr/bin/env python3
"""Smoke-test the built Sandbox image through the Runtime JSONL boundary."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
import re
import tempfile
import uuid


DEFAULT_TIMEOUT_SECONDS = 60.0
PROTOCOL_VERSION = 1


class SmokeError(RuntimeError):
    pass


def parse_smoke_jsonl(lines: list[bytes]) -> list[dict[str, object]]:
    """Parse and validate every stdout line emitted by the Runtime process."""
    messages: list[dict[str, object]] = []
    for line_number, line in enumerate(lines, start=1):
        if not line.strip():
            raise SmokeError(f"Sandbox stdout line {line_number} is empty.")
        try:
            text = line.decode("utf-8")
            payload = json.loads(text)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise SmokeError(
                f"Sandbox stdout line {line_number} is not valid UTF-8 JSONL."
            ) from error
        if not isinstance(payload, dict):
            raise SmokeError(
                f"Sandbox stdout line {line_number} is not a JSON object."
            )
        if payload.get("version") != PROTOCOL_VERSION or isinstance(
            payload.get("version"), bool
        ):
            raise SmokeError(
                f"Sandbox stdout line {line_number} has an unsupported protocol version."
            )
        message_type = payload.get("type")
        if not isinstance(message_type, str) or not message_type.strip():
            raise SmokeError(
                f"Sandbox stdout line {line_number} has no message type."
            )
        messages.append(payload)
    return messages


def validate_smoke_messages(
    messages: list[dict[str, object]], return_code: int
) -> None:
    """Require the complete startup/close handshake and a clean exit."""
    message_types = [message.get("type") for message in messages]
    if "runtime_ready" not in message_types:
        raise SmokeError("Sandbox smoke did not receive runtime_ready.")
    if "runtime_closed" not in message_types:
        raise SmokeError("Sandbox smoke did not receive runtime_closed.")
    if message_types.index("runtime_ready") > message_types.index("runtime_closed"):
        raise SmokeError("Sandbox smoke received runtime_closed before runtime_ready.")
    if return_code != 0:
        raise SmokeError(
            f"Sandbox smoke process exited with non-zero code {return_code}."
        )


def _redact_diagnostic(value: str) -> str:
    value = re.sub(
        r"(?i)(authorization|api[_-]?key|token|secret|password)(\s*[:=]\s*)\S+",
        r"\1\2[redacted]",
        value,
    )
    value = re.sub(r"\b(?:sk|rk)-[A-Za-z0-9_-]{10,}\b", "[redacted]", value)
    compact = value.replace("\r", " ").replace("\n", " ").strip()
    return compact if len(compact) <= 4_000 else compact[:3_997] + "..."


async def _terminate_process(process: asyncio.subprocess.Process) -> None:
    if process.returncode is not None:
        return
    try:
        process.terminate()
    except ProcessLookupError:
        return
    try:
        await asyncio.wait_for(process.wait(), timeout=5)
        return
    except (TimeoutError, ProcessLookupError):
        pass
    if process.returncode is None:
        process.kill()
        await process.wait()


async def _remove_container(docker_command: str, container_name: str) -> None:
    try:
        cleanup = await asyncio.create_subprocess_exec(
            docker_command,
            "rm",
            "-f",
            container_name,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await asyncio.wait_for(cleanup.wait(), timeout=10)
    except (OSError, TimeoutError):
        pass


async def run_smoke(
    *,
    image: str,
    docker_command: str = "docker",
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
) -> None:
    if not image.strip():
        raise SmokeError("Sandbox image must not be empty.")
    if timeout_seconds <= 0:
        raise SmokeError("Smoke timeout must be greater than zero.")

    container_name = f"mycode-web-sandbox-smoke-{uuid.uuid4().hex[:12]}"
    process: asyncio.subprocess.Process | None = None
    stderr_task: asyncio.Task[bytes] | None = None
    captured_lines: list[bytes] = []
    captured_stderr = b""

    with tempfile.TemporaryDirectory(prefix="mycode-web-sandbox-smoke-") as root:
        root_path = Path(root)
        workspace = root_path / "workspace"
        state = root_path / "mycode-state"
        workspace.mkdir()
        state.mkdir()
        # The image runs as uid 10001, while the host temp directory belongs to
        # the deploy user. The directories contain no secrets and are removed
        # with the TemporaryDirectory context after the container exits.
        workspace.chmod(0o777)
        state.chmod(0o777)

        command = [
            docker_command,
            "run",
            "--rm",
            "-i",
            "--name",
            container_name,
            "--network",
            "none",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--pids-limit",
            "128",
            "--memory",
            "512m",
            "--mount",
            f"type=bind,source={workspace},target=/workspace",
            "--mount",
            f"type=bind,source={state},target=/home/mycode/.mycode",
            "--env",
            "HOME=/home/mycode",
            "--env",
            "PYTHONUNBUFFERED=1",
            "--env",
            "MYCODE_API_KEY=smoke-dummy-key",
            "--env",
            "MYCODE_BASE_URL=http://127.0.0.1:9/v1",
            "--env",
            "MYCODE_MODEL=smoke-model",
            image,
            "mycode",
            "runtime",
            "--jsonl",
            "--new",
        ]

        try:
            try:
                process = await asyncio.create_subprocess_exec(
                    *command,
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
            except OSError as error:
                raise SmokeError(
                    f"Could not start Docker for Sandbox smoke: {type(error).__name__}."
                ) from error

            assert process.stdout is not None
            assert process.stderr is not None
            stderr_task = asyncio.create_task(process.stderr.read())
            deadline = asyncio.get_running_loop().time() + timeout_seconds
            close_sent = False
            runtime_closed = False

            while not runtime_closed:
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    raise SmokeError("Sandbox smoke timed out waiting for JSONL output.")
                try:
                    line = await asyncio.wait_for(process.stdout.readline(), remaining)
                except TimeoutError as error:
                    raise SmokeError(
                        "Sandbox smoke timed out waiting for JSONL output."
                    ) from error
                if not line:
                    break
                captured_lines.append(line)
                message = parse_smoke_jsonl([line])[0]
                if message["type"] == "runtime_ready" and not close_sent:
                    if process.stdin is None:
                        raise SmokeError("Sandbox smoke stdin is unavailable.")
                    process.stdin.write(b'{"version":1,"type":"close"}\n')
                    await process.stdin.drain()
                    close_sent = True
                if message["type"] == "runtime_closed":
                    runtime_closed = True

            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                raise SmokeError("Sandbox smoke timed out waiting for process exit.")
            return_code = await asyncio.wait_for(process.wait(), remaining)
            if stderr_task is not None:
                captured_stderr = await stderr_task
            messages = parse_smoke_jsonl(captured_lines)
            validate_smoke_messages(messages, return_code)
        except SmokeError as error:
            if process is not None:
                await _terminate_process(process)
            if stderr_task is not None and not stderr_task.done():
                try:
                    captured_stderr = await asyncio.wait_for(stderr_task, timeout=2)
                except TimeoutError:
                    stderr_task.cancel()
                    await asyncio.gather(stderr_task, return_exceptions=True)
            detail = _redact_diagnostic(captured_stderr.decode("utf-8", errors="replace"))
            suffix = f" stderr={detail}" if detail else ""
            raise SmokeError(f"{error}{suffix}") from error
        finally:
            if process is not None and process.returncode is None:
                await _terminate_process(process)
            if stderr_task is not None and not stderr_task.done():
                stderr_task.cancel()
                await asyncio.gather(stderr_task, return_exceptions=True)
            await _remove_container(docker_command, container_name)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", default=os.getenv("MYCODE_SANDBOX_IMAGE", "mycode-sandbox:dev"))
    parser.add_argument("--docker-command", default=os.getenv("MYCODE_DOCKER_COMMAND", "docker"))
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_SECONDS)
    args = parser.parse_args()
    try:
        asyncio.run(
            run_smoke(
                image=args.image,
                docker_command=args.docker_command,
                timeout_seconds=args.timeout,
            )
        )
    except SmokeError as error:
        print(f"ERROR: Sandbox compatibility smoke failed: {error}", flush=True)
        return 1
    print("Sandbox compatibility smoke PASS: runtime_ready -> runtime_closed (exit 0)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
