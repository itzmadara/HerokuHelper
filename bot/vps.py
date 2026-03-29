from __future__ import annotations

from dataclasses import dataclass
import re
import shlex
from uuid import uuid4

import asyncssh


class VPSAPIError(Exception):
    """Raised when a VPS SSH operation fails."""


SESSION_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]+$")


@dataclass(slots=True)
class VPSServerConfig:
    name: str
    host: str
    username: str
    password: str
    port: int = 22


@dataclass(slots=True)
class ScreenBotConfig:
    label: str
    session_name: str
    workdir: str
    start_command: str


@dataclass(slots=True)
class DockerBotConfig:
    label: str
    container_name: str


class VPSClient:
    @staticmethod
    def _validate_session_name(session_name: str) -> None:
        if not SESSION_NAME_RE.fullmatch(session_name):
            raise VPSAPIError(
                "Session name can only use letters, numbers, dot, underscore, and dash."
            )

    async def _run(self, server: VPSServerConfig, command: str, *, check: bool = True) -> str:
        try:
            async with asyncssh.connect(
                server.host,
                port=server.port,
                username=server.username,
                password=server.password,
                known_hosts=None,
            ) as connection:
                result = await connection.run(command, check=False)
        except (asyncssh.Error, OSError) as exc:
            raise VPSAPIError(f"SSH connection failed: {exc}") from exc

        output = (result.stdout or "").strip()
        error = (result.stderr or "").strip()
        if check and result.exit_status != 0:
            raise VPSAPIError(error or output or f"Remote command failed with exit code {result.exit_status}.")

        return output or error

    async def test_connection(self, server: VPSServerConfig) -> str:
        command = "bash -lc " + shlex.quote(
            "printf 'host=%s\nuser=%s\n' \"$(hostname)\" \"$(whoami)\"; "
            "if command -v screen >/dev/null 2>&1; then screen -v; else echo 'screen=missing'; fi; "
            "if command -v docker >/dev/null 2>&1; then docker --version; else echo 'docker=missing'; fi"
        )
        return await self._run(server, command)

    async def list_docker_containers(self, server: VPSServerConfig, *, all_containers: bool = False) -> list[str]:
        all_flag = "-a " if all_containers else ""
        command = "bash -lc " + shlex.quote(
            "command -v docker >/dev/null 2>&1 || { echo 'docker is not installed.' >&2; exit 1; }; "
            f"docker ps {all_flag}--format '{{{{.Names}}}}|{{{{.Status}}}}'"
        )
        output = await self._run(server, command)
        return [line.strip() for line in output.splitlines() if line.strip()]

    async def docker_container_status(self, server: VPSServerConfig, container_name: str) -> str:
        command = "bash -lc " + shlex.quote(
            "command -v docker >/dev/null 2>&1 || { echo 'docker is not installed.' >&2; exit 1; }; "
            f"docker inspect -f '{{{{.State.Status}}}}' {shlex.quote(container_name)}"
        )
        return await self._run(server, command)

    async def start_docker_bot(self, server: VPSServerConfig, bot: DockerBotConfig) -> None:
        command = "bash -lc " + shlex.quote(
            "command -v docker >/dev/null 2>&1 || { echo 'docker is not installed.' >&2; exit 1; }; "
            f"docker start {shlex.quote(bot.container_name)}"
        )
        await self._run(server, command)

    async def stop_docker_bot(self, server: VPSServerConfig, container_name: str) -> None:
        command = "bash -lc " + shlex.quote(
            "command -v docker >/dev/null 2>&1 || { echo 'docker is not installed.' >&2; exit 1; }; "
            f"docker stop {shlex.quote(container_name)}"
        )
        await self._run(server, command)

    async def restart_docker_bot(self, server: VPSServerConfig, bot: DockerBotConfig) -> None:
        command = "bash -lc " + shlex.quote(
            "command -v docker >/dev/null 2>&1 || { echo 'docker is not installed.' >&2; exit 1; }; "
            f"docker restart {shlex.quote(bot.container_name)}"
        )
        await self._run(server, command)

    async def docker_logs(self, server: VPSServerConfig, container_name: str, *, tail: int = 120) -> str:
        command = "bash -lc " + shlex.quote(
            "command -v docker >/dev/null 2>&1 || { echo 'docker is not installed.' >&2; exit 1; }; "
            f"docker logs --tail {int(tail)} {shlex.quote(container_name)} 2>&1"
        )
        return await self._run(server, command)

    async def list_screen_sessions(self, server: VPSServerConfig) -> list[str]:
        command = "bash -lc " + shlex.quote(
            "command -v screen >/dev/null 2>&1 || { echo 'screen is not installed.' >&2; exit 1; }; "
            "screen -ls 2>/dev/null || true"
        )
        output = await self._run(server, command)
        sessions: list[str] = []
        for line in output.splitlines():
            match = re.match(r"^\s*(\d+\.[^\s]+)\s+\(", line)
            if match:
                sessions.append(match.group(1))
        return sessions

    async def is_session_running(self, server: VPSServerConfig, session_name: str) -> bool:
        self._validate_session_name(session_name)
        command = "bash -lc " + shlex.quote(
            "command -v screen >/dev/null 2>&1 || { echo 'screen is not installed.' >&2; exit 1; }; "
            f"if screen -ls 2>/dev/null | grep -F {shlex.quote(f'.{session_name}')} >/dev/null 2>&1; "
            "then echo running; else echo stopped; fi"
        )
        output = await self._run(server, command)
        return output.strip() == "running"

    async def start_screen_bot(self, server: VPSServerConfig, bot: ScreenBotConfig) -> None:
        self._validate_session_name(bot.session_name)
        if await self.is_session_running(server, bot.session_name):
            raise VPSAPIError("This screen session is already running.")

        command = "bash -lc " + shlex.quote(
            f"cd {shlex.quote(bot.workdir)} && "
            f"screen -dmS {shlex.quote(bot.session_name)} bash -lc {shlex.quote(bot.start_command)}"
        )
        await self._run(server, command)

    async def stop_screen_bot(self, server: VPSServerConfig, session_name: str) -> None:
        self._validate_session_name(session_name)
        if not await self.is_session_running(server, session_name):
            raise VPSAPIError("This screen session is not running.")

        command = "bash -lc " + shlex.quote(
            f"screen -S {shlex.quote(session_name)} -X quit"
        )
        await self._run(server, command)

    async def restart_screen_bot(self, server: VPSServerConfig, bot: ScreenBotConfig) -> None:
        if await self.is_session_running(server, bot.session_name):
            await self.stop_screen_bot(server, bot.session_name)
        await self.start_screen_bot(server, bot)

    async def capture_screen(self, server: VPSServerConfig, session_name: str) -> str:
        self._validate_session_name(session_name)
        if not await self.is_session_running(server, session_name):
            raise VPSAPIError("This screen session is not running.")

        tmp_path = f"/tmp/screen-capture-{uuid4().hex}.txt"
        command = "bash -lc " + shlex.quote(
            f"trap 'rm -f {shlex.quote(tmp_path)}' EXIT; "
            f"screen -S {shlex.quote(session_name)} -X hardcopy -h {shlex.quote(tmp_path)} && "
            f"cat {shlex.quote(tmp_path)}"
        )
        return await self._run(server, command)
