from __future__ import annotations

from contextlib import suppress
from dataclasses import dataclass
import json
import posixpath
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


@dataclass(slots=True)
class ScreenSetupGuess:
    workdir: str
    command: str
    pid: int | None = None


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

    async def _connect(self, server: VPSServerConfig) -> asyncssh.SSHClientConnection:
        try:
            return await asyncssh.connect(
                server.host,
                port=server.port,
                username=server.username,
                password=server.password,
                known_hosts=None,
            )
        except (asyncssh.Error, OSError) as exc:
            raise VPSAPIError(f"SSH connection failed: {exc}") from exc

    def _session_locator_command(self, session_name: str) -> str:
        self._validate_session_name(session_name)
        return (
            "command -v screen >/dev/null 2>&1 || { echo 'screen is not installed.' >&2; exit 1; }; "
            f"screen -ls 2>/dev/null | awk '$1 ~ /^[0-9]+\\.{re.escape(session_name)}$/ "
            "{split($1,a,\".\"); print a[1]; exit}'"
        )

    async def _path_kind(self, connection: asyncssh.SSHClientConnection, remote_path: str) -> str:
        command = "bash -lc " + shlex.quote(
            f"if [ -d {shlex.quote(remote_path)} ]; then echo dir; "
            f"elif [ -f {shlex.quote(remote_path)} ]; then echo file; "
            "else echo missing; fi"
        )
        result = await connection.run(command, check=False)
        return (result.stdout or "").strip() or "missing"

    async def copy_paths_between_servers(
        self,
        source: VPSServerConfig,
        target: VPSServerConfig,
        paths: list[str],
    ) -> dict[str, list[str]]:
        normalized_paths: list[str] = []
        seen_paths: set[str] = set()
        for path in paths:
            clean_path = str(path).strip()
            if not clean_path or clean_path in seen_paths:
                continue
            normalized_paths.append(clean_path)
            seen_paths.add(clean_path)

        result = {
            "copied": [],
            "skipped": [],
            "missing": [],
            "failed": [],
        }
        if not normalized_paths:
            return result

        source_connection = await self._connect(source)
        try:
            target_connection = await self._connect(target)
        except Exception:
            source_connection.close()
            await source_connection.wait_closed()
            raise

        try:
            for remote_path in normalized_paths:
                source_kind = await self._path_kind(source_connection, remote_path)
                if source_kind == "missing":
                    result["missing"].append(remote_path)
                    continue

                target_kind = await self._path_kind(target_connection, remote_path)
                if target_kind != "missing":
                    result["skipped"].append(remote_path)
                    continue

                source_path = remote_path.rstrip("/") or "/"
                parent_path = posixpath.dirname(source_path) or "/"
                base_name = posixpath.basename(source_path)
                if not base_name:
                    result["failed"].append(f"{remote_path} (root path is not supported)")
                    continue

                source_command = "bash -lc " + shlex.quote(
                    f"tar -C {shlex.quote(parent_path)} -czf - {shlex.quote(base_name)}"
                )
                target_command = "bash -lc " + shlex.quote(
                    f"mkdir -p {shlex.quote(parent_path)} && tar -xzf - -C {shlex.quote(parent_path)}"
                )

                source_process = await source_connection.create_process(source_command, encoding=None)
                target_process = await target_connection.create_process(target_command, encoding=None)

                try:
                    while True:
                        chunk = await source_process.stdout.read(65536)
                        if not chunk:
                            break
                        target_process.stdin.write(chunk)
                    target_process.stdin.write_eof()

                    source_result = await source_process.wait(check=False)
                    target_result = await target_process.wait(check=False)
                    if source_result.exit_status != 0:
                        error = (source_result.stderr or b"").decode("utf-8", "ignore").strip()
                        raise VPSAPIError(error or f"Failed to archive {remote_path} on source VPS.")
                    if target_result.exit_status != 0:
                        error = (target_result.stderr or b"").decode("utf-8", "ignore").strip()
                        raise VPSAPIError(error or f"Failed to extract {remote_path} on target VPS.")
                except Exception as exc:
                    with suppress(Exception):
                        target_process.stdin.write_eof()
                    with suppress(Exception):
                        source_process.close()
                    with suppress(Exception):
                        target_process.close()
                    result["failed"].append(f"{remote_path} ({exc})")
                    continue

                result["copied"].append(remote_path)
        finally:
            source_connection.close()
            target_connection.close()
            await source_connection.wait_closed()
            await target_connection.wait_closed()

        return result

    async def test_connection(self, server: VPSServerConfig) -> str:
        command = "bash -lc " + shlex.quote(
            "printf 'host=%s\nuser=%s\n' \"$(hostname)\" \"$(whoami)\"; "
            "if command -v screen >/dev/null 2>&1; then screen -v; else echo 'screen=missing'; fi; "
            "if command -v docker >/dev/null 2>&1; then docker --version; else echo 'docker=missing'; fi"
        )
        return await self._run(server, command)

    async def list_docker_containers(
        self,
        server: VPSServerConfig,
        *,
        all_containers: bool = False,
    ) -> list[dict[str, str]]:
        all_flag = "-a " if all_containers else ""
        command = "bash -lc " + shlex.quote(
            "command -v docker >/dev/null 2>&1 || { echo 'docker is not installed.' >&2; exit 1; }; "
            f"docker ps {all_flag}--format '{{{{.Names}}}}|{{{{.Image}}}}|{{{{.State}}}}|{{{{.Status}}}}'"
        )
        output = await self._run(server, command)
        containers: list[dict[str, str]] = []
        for line in output.splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            name, _, rest = stripped.partition("|")
            image, _, rest = rest.partition("|")
            state, _, status = rest.partition("|")
            containers.append(
                {
                    "name": name.strip(),
                    "image": image.strip(),
                    "state": state.strip(),
                    "status": status.strip(),
                }
            )
        return containers

    async def list_stopped_docker_containers(self, server: VPSServerConfig) -> list[str]:
        command = "bash -lc " + shlex.quote(
            "command -v docker >/dev/null 2>&1 || { echo 'docker is not installed.' >&2; exit 1; }; "
            "docker ps -a "
            "--filter status=created "
            "--filter status=exited "
            "--filter status=dead "
            "--format '{{.Names}}'"
        )
        output = await self._run(server, command)
        return [line.strip() for line in output.splitlines() if line.strip()]

    async def remove_stopped_docker_containers(self, server: VPSServerConfig) -> list[str]:
        container_names = await self.list_stopped_docker_containers(server)
        if not container_names:
            return []

        quoted_names = " ".join(shlex.quote(name) for name in container_names)
        command = "bash -lc " + shlex.quote(
            "command -v docker >/dev/null 2>&1 || { echo 'docker is not installed.' >&2; exit 1; }; "
            f"docker rm {quoted_names}"
        )
        output = await self._run(server, command)
        removed_names = [line.strip() for line in output.splitlines() if line.strip()]
        return removed_names or container_names

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

    async def auto_detect_screen_setup(self, server: VPSServerConfig, session_name: str) -> ScreenSetupGuess:
        self._validate_session_name(session_name)
        remote_script = f"""
set -e
session_pid="$({self._session_locator_command(session_name)})"
if [ -z "$session_pid" ]; then
  echo "Screen session not found." >&2
  exit 1
fi
if command -v python3 >/dev/null 2>&1; then
  py_bin=python3
elif command -v python >/dev/null 2>&1; then
  py_bin=python
else
  echo "Python is required on the VPS to auto-detect screen setup." >&2
  exit 1
fi
SESSION_PID="$session_pid" "$py_bin" - <<'PY'
import json
import os
from pathlib import Path

session_pid = int(os.environ["SESSION_PID"])
wrapper_names = {{"screen", "screen-4.09.00", "bash", "sh", "dash", "zsh", "fish", "sudo", "su"}}
preferred_names = {{"python", "python3", "node", "npm", "java", "go", "ruby", "php", "perl", "bun"}}

def children(pid: int) -> list[int]:
    try:
        text = Path(f"/proc/{{pid}}/task/{{pid}}/children").read_text().strip()
    except Exception:
        return []
    return [int(part) for part in text.split() if part.isdigit()]

def cmdline(pid: int) -> list[str]:
    try:
        data = Path(f"/proc/{{pid}}/cmdline").read_bytes()
    except Exception:
        return []
    return [part.decode("utf-8", "ignore") for part in data.split(b"\\0") if part]

def cwd(pid: int) -> str:
    try:
        return os.readlink(f"/proc/{{pid}}/cwd")
    except Exception:
        return ""

seen = set()
queue: list[tuple[int, int]] = [(session_pid, 0)]
best: dict | None = None
best_score = -10**9

while queue:
    pid, depth = queue.pop(0)
    if pid in seen:
        continue
    seen.add(pid)
    for child in children(pid):
        queue.append((child, depth + 1))

    args = cmdline(pid)
    if not args:
        continue

    exe_name = os.path.basename(args[0]).lower()
    current_cwd = cwd(pid)
    joined = " ".join(args).strip()
    score = depth * 100

    if current_cwd:
        score += 20
    if exe_name and exe_name not in wrapper_names:
        score += 80
    if exe_name in preferred_names:
        score += 120
    if any(token in joined for token in ("main.py", ".py", "start.sh", "node ", "python", "npm", "java ")):
        score += 40
    if pid == session_pid:
        score -= 200

    if score > best_score:
        best_score = score
        best = {{
            "pid": pid,
            "workdir": current_cwd,
            "command": joined,
        }}

if not best or not best.get("workdir") or not best.get("command"):
    raise SystemExit("Unable to auto-detect workdir/command for this screen session.")

print(json.dumps(best))
PY
"""
        output = await self._run(server, "bash -lc " + shlex.quote(remote_script))
        try:
            payload = json.loads(output)
        except json.JSONDecodeError as exc:
            raise VPSAPIError("Unable to parse auto-detected screen setup.") from exc

        workdir = str(payload.get("workdir", "")).strip()
        command = str(payload.get("command", "")).strip()
        if not workdir or not command:
            raise VPSAPIError("Unable to auto-detect workdir and command for this screen session.")

        pid_value = payload.get("pid")
        pid = int(pid_value) if isinstance(pid_value, int | float) or str(pid_value).isdigit() else None
        return ScreenSetupGuess(workdir=workdir, command=command, pid=pid)

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
