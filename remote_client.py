import httpx

from command_runner import CommandResult
from system_stats import ResourceSnapshot, snapshot_from_dict


class RemoteAgentError(Exception):
    pass


async def fetch_remote_status(
    url: str,
    token: str,
    *,
    timeout: float = 15.0,
) -> ResourceSnapshot:
    headers = {"Authorization": f"Bearer {token}"}
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.get(f"{url.rstrip('/')}/status", headers=headers)
            response.raise_for_status()
            data = response.json()
    except httpx.HTTPError as exc:
        raise RemoteAgentError(str(exc)) from exc

    if not isinstance(data, dict):
        raise RemoteAgentError("Invalid response from agent")
    try:
        return snapshot_from_dict(data)
    except (KeyError, TypeError, ValueError) as exc:
        raise RemoteAgentError("Agent returned malformed status data") from exc


async def ping_agent(url: str, token: str, *, timeout: float = 5.0) -> bool:
    headers = {"Authorization": f"Bearer {token}"}
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.get(f"{url.rstrip('/')}/health", headers=headers)
            return response.status_code == 200
    except httpx.HTTPError:
        return False


async def run_remote_command(
    url: str,
    token: str,
    command: str,
    *,
    timeout: float = 30.0,
    max_output: int = 3500,
) -> CommandResult:
    headers = {"Authorization": f"Bearer {token}"}
    payload = {
        "command": command,
        "timeout": timeout,
        "max_output": max_output,
    }
    try:
        async with httpx.AsyncClient(timeout=timeout + 5.0) as client:
            response = await client.post(
                f"{url.rstrip('/')}/exec",
                headers=headers,
                json=payload,
            )
            response.raise_for_status()
            data = response.json()
    except httpx.HTTPError as exc:
        raise RemoteAgentError(str(exc)) from exc

    if not isinstance(data, dict):
        raise RemoteAgentError("Invalid response from agent")
    try:
        return CommandResult(
            stdout=str(data.get("stdout", "")),
            stderr=str(data.get("stderr", "")),
            exit_code=int(data.get("exit_code", 1)),
            timed_out=bool(data.get("timed_out", False)),
        )
    except (TypeError, ValueError) as exc:
        raise RemoteAgentError("Agent returned malformed exec response") from exc
