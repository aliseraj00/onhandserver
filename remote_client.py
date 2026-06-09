import httpx

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
