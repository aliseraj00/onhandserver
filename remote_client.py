import json
from collections.abc import Awaitable, Callable

import httpx

from command_runner import CommandCancel, CommandResult
from system_stats import ResourceSnapshot, snapshot_from_dict

OutputCallback = Callable[[str, str], Awaitable[None] | None]


class RemoteAgentError(Exception):
    pass


class RemoteExecHandle:
    """Allows the bot to Ctrl+C a remote command by closing the HTTP stream."""

    def __init__(self) -> None:
        self._response: httpx.Response | None = None
        self._cancel = CommandCancel()

    @property
    def cancel(self) -> CommandCancel:
        return self._cancel

    def bind_response(self, response: httpx.Response) -> None:
        self._response = response
        if self._cancel.is_set():
            self._close_response()

    def interrupt(self) -> None:
        self._cancel.interrupt()
        self._close_response()

    def _close_response(self) -> None:
        response = self._response
        if response is None:
            return
        try:
            response.close()
        except Exception:
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
    timeout: float = 300.0,
    max_output: int = 3500,
    on_output: OutputCallback | None = None,
    handle: RemoteExecHandle | None = None,
    cwd: str | None = None,
) -> CommandResult:
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/x-ndjson",
    }
    payload: dict = {
        "command": command,
        "timeout": timeout,
        "max_output": max_output,
    }
    if cwd:
        payload["cwd"] = cwd
    http_timeout = httpx.Timeout(
        connect=10.0,
        read=timeout + 30.0,
        write=30.0,
        pool=10.0,
    )
    exec_handle = handle or RemoteExecHandle()
    try:
        async with httpx.AsyncClient(timeout=http_timeout) as client:
            async with client.stream(
                "POST",
                f"{url.rstrip('/')}/exec",
                headers=headers,
                json=payload,
            ) as response:
                exec_handle.bind_response(response)
                if response.status_code >= 400:
                    body = (await response.aread()).decode("utf-8", errors="replace")
                    try:
                        data = json.loads(body)
                        detail = data.get("error", body)
                    except json.JSONDecodeError:
                        detail = body or response.reason_phrase
                    raise RemoteAgentError(f"HTTP {response.status_code}: {detail}")

                content_type = response.headers.get("content-type", "")
                if "application/x-ndjson" in content_type:
                    return await _consume_ndjson_stream(
                        response,
                        on_output=on_output,
                        handle=exec_handle,
                        fallback_cwd=cwd,
                    )

                body = await response.aread()
                data = json.loads(body.decode("utf-8"))
                if not isinstance(data, dict):
                    raise RemoteAgentError("Invalid response from agent")
                result = CommandResult(
                    stdout=str(data.get("stdout", "")),
                    stderr=str(data.get("stderr", "")),
                    exit_code=int(data.get("exit_code", 1)),
                    timed_out=bool(data.get("timed_out", False)),
                    cancelled=bool(data.get("cancelled", False)),
                    cwd=(str(data["cwd"]) if data.get("cwd") else cwd),
                )
                if on_output is not None:
                    if result.stdout:
                        maybe = on_output("stdout", result.stdout)
                        if maybe is not None:
                            await maybe
                    if result.stderr:
                        maybe = on_output("stderr", result.stderr)
                        if maybe is not None:
                            await maybe
                return result
    except RemoteAgentError:
        raise
    except (httpx.HTTPError, httpx.StreamError) as exc:
        if exec_handle.cancel.is_set():
            return CommandResult("", "Stopped (Ctrl+C)", 130, False, True, cwd)
        raise RemoteAgentError(str(exc)) from exc
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        raise RemoteAgentError("Agent returned malformed exec response") from exc


async def _consume_ndjson_stream(
    response: httpx.Response,
    *,
    on_output: OutputCallback | None,
    handle: RemoteExecHandle,
    fallback_cwd: str | None = None,
) -> CommandResult:
    result: CommandResult | None = None
    try:
        async for line in response.aiter_lines():
            if handle.cancel.is_set():
                break
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError as exc:
                raise RemoteAgentError("Agent returned malformed stream line") from exc
            if not isinstance(event, dict):
                raise RemoteAgentError("Agent returned malformed stream event")

            kind = event.get("event")
            if kind == "out":
                stream = str(event.get("stream", "stdout"))
                text = str(event.get("text", ""))
                if text and on_output is not None:
                    maybe = on_output(stream, text)
                    if maybe is not None:
                        await maybe
            elif kind == "done":
                cwd_val = event.get("cwd")
                result = CommandResult(
                    stdout=str(event.get("stdout", "")),
                    stderr=str(event.get("stderr", "")),
                    exit_code=int(event.get("exit_code", 1)),
                    timed_out=bool(event.get("timed_out", False)),
                    cancelled=bool(event.get("cancelled", False)),
                    cwd=(str(cwd_val) if cwd_val else fallback_cwd),
                )
            elif kind == "error":
                raise RemoteAgentError(str(event.get("error", "agent exec failed")))
            else:
                raise RemoteAgentError(f"Unknown stream event: {kind!r}")
    except (httpx.HTTPError, httpx.StreamError):
        if handle.cancel.is_set():
            return CommandResult("", "Stopped (Ctrl+C)", 130, False, True, fallback_cwd)
        raise

    if handle.cancel.is_set() and (result is None or not result.cancelled):
        return CommandResult(
            result.stdout if result else "",
            (result.stderr + "\nStopped (Ctrl+C)").strip() if result else "Stopped (Ctrl+C)",
            130,
            False,
            True,
            result.cwd if result else fallback_cwd,
        )
    if result is None:
        raise RemoteAgentError("Agent closed stream without a done event")
    return result
