import html
import os
import re
import select
import shlex
import signal
import subprocess
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass

try:
    import pty
except ImportError:  # pragma: no cover
    pty = None  # type: ignore

OutputCallback = Callable[[str, str], None]

_CWD_MARKER = "__OHS_CWD__:"

# Full-screen / interactive TUI tools cannot be driven from Telegram.
_INTERACTIVE_BASENAMES = frozenset(
    {
        "nano",
        "vim",
        "vi",
        "nvim",
        "emacs",
        "emacsclient",
        "pico",
        "micro",
        "joe",
        "less",
        "more",
        "most",
        "top",
        "htop",
        "btop",
        "atop",
        "watch",
        "tmux",
        "screen",
        "ftp",
        "sftp",
        "mysql",
        "psql",
    }
)

_REPL_BASENAMES = frozenset({"python", "python3", "node", "php", "irb", "sqlite3"})

_INTERACTIVE_HELP = (
    "Interactive programs (nano, vim, top, …) cannot run over Telegram.\n\n"
    "Edit or view files with non-interactive commands instead, e.g.:\n"
    "  cat /path/to/file\n"
    "  cat > /path/to/file <<'EOF'\n"
    "  paste content here\n"
    "  EOF\n"
    "  printf '%s\\n' 'line' >> /path/to/file\n"
    "  sed -i 's/old/new/' /path/to/file"
)


@dataclass(frozen=True)
class CommandResult:
    stdout: str
    stderr: str
    exit_code: int
    timed_out: bool
    cancelled: bool = False
    cwd: str | None = None


class CommandCancel:
    """Signal a running command to stop (Ctrl+C / SIGINT, then SIGKILL)."""

    def __init__(self) -> None:
        self._event = threading.Event()
        self._proc: subprocess.Popen | None = None
        self._tty_fd: int | None = None
        self._lock = threading.Lock()
        self._escalator: threading.Thread | None = None

    def bind(self, proc: subprocess.Popen, *, tty_fd: int | None = None) -> None:
        with self._lock:
            self._proc = proc
            self._tty_fd = tty_fd
            if self._event.is_set():
                self._deliver_interrupt_unlocked()

    def clear_tty(self) -> None:
        with self._lock:
            self._tty_fd = None

    def is_set(self) -> bool:
        return self._event.is_set()

    def interrupt(self) -> None:
        """Ctrl+C: request cancel and deliver interrupt to the process/PTY."""
        self._event.set()
        with self._lock:
            self._deliver_interrupt_unlocked()
            if self._escalator is None or not self._escalator.is_alive():
                self._escalator = threading.Thread(
                    target=self._escalate, daemon=True
                )
                self._escalator.start()

    def kill(self) -> None:
        self._event.set()
        with self._lock:
            proc = self._proc
        if proc is not None:
            _kill_process_group(proc)

    def _deliver_interrupt_unlocked(self) -> None:
        tty_fd = self._tty_fd
        proc = self._proc
        # Real terminal Ctrl+C for PTY-backed commands.
        if tty_fd is not None:
            try:
                os.write(tty_fd, b"\x03")
            except OSError:
                pass
        if proc is not None:
            _signal_process_group(proc, signal.SIGINT)

    def _escalate(self) -> None:
        time.sleep(0.35)
        if not self._event.is_set():
            return
        with self._lock:
            proc = self._proc
            tty_fd = self._tty_fd
        if tty_fd is not None:
            try:
                os.write(tty_fd, b"\x03")
            except OSError:
                pass
        if proc is not None and proc.poll() is None:
            _signal_process_group(proc, signal.SIGTERM)
        time.sleep(0.35)
        if not self._event.is_set():
            return
        with self._lock:
            proc = self._proc
        if proc is not None and proc.poll() is None:
            _kill_process_group(proc)


def interactive_command_block_reason(command: str) -> str | None:
    """Return a help message if command looks like an interactive TUI / REPL."""
    cmd = command.strip()
    if not cmd:
        return None
    for segment in re.split(r"[|;&\n]+", cmd):
        tokens = _leading_command_tokens(segment)
        if not tokens:
            continue
        base = os.path.basename(tokens[0].rstrip("/"))
        if base in _INTERACTIVE_BASENAMES:
            return _INTERACTIVE_HELP
        if base in _REPL_BASENAMES and len(tokens) == 1:
            return _INTERACTIVE_HELP
    return None


def _leading_command_tokens(segment: str) -> list[str]:
    cleaned = re.sub(r"""('([^']*)'|"([^"]*)")""", " ", segment)
    tokens = cleaned.split()
    out: list[str] = []
    i = 0
    while i < len(tokens):
        t = tokens[i]
        if "=" in t and not t.startswith("-") and t.index("=") > 0:
            i += 1
            continue
        if t in {"sudo", "doas", "command", "time", "nice", "nohup", "env"}:
            i += 1
            continue
        out = tokens[i:]
        break
    return out


def normalize_shell_command(command: str) -> str:
    """Fix common Telegram typos: unquoted cd paths with spaces.

    Example: cd Developer - AliAkbar  →  cd 'Developer - AliAkbar'
    Leaves already-quoted paths and compound commands (&&, |, ;) alone.
    """
    cmd = command.strip()
    if not cmd:
        return command
    if not re.match(r"^cd\b", cmd):
        return command
    if re.search(r"(&&|\|\||[|;\n])", cmd):
        return command

    try:
        parts = shlex.split(cmd)
    except ValueError:
        return command
    if not parts or parts[0] != "cd":
        return command

    args = parts[1:]
    i = 0
    while i < len(args):
        arg = args[i]
        if arg == "--":
            i += 1
            break
        # Keep real cd flags (-L, -P, …). Bare "-" is the previous-dir operand.
        if arg.startswith("-") and arg not in {"-", "--"}:
            i += 1
            continue
        break

    path_parts = args[i:]
    if len(path_parts) <= 1:
        return command

    joined = " ".join(path_parts)
    flags = args[:i]
    if flags:
        return "cd " + " ".join(shlex.quote(f) for f in flags) + " " + shlex.quote(joined)
    return "cd " + shlex.quote(joined)


def run_command(
    command: str,
    *,
    timeout: float = 300.0,
    max_output: int = 3500,
    on_output: OutputCallback | None = None,
    cancel: CommandCancel | None = None,
    cwd: str | None = None,
) -> CommandResult:
    """Run a shell command, streaming output via on_output when provided.

    When cwd is set, the command runs in that directory and the resulting
    working directory (after any cd in the command) is returned on result.cwd.
    """
    command = normalize_shell_command(command.strip())
    if not command:
        return CommandResult("", "Empty command", 1, False, cwd=cwd)

    blocked = interactive_command_block_reason(command)
    if blocked:
        return CommandResult("", blocked, 1, False, cwd=cwd)

    if cancel is None:
        cancel = CommandCancel()

    tracker: _CwdOutputFilter | None = None
    run_script = command
    if cwd:
        run_script, tracker = _wrap_with_cwd(command, cwd, on_output)
        stream_cb = tracker.on_output
    else:
        stream_cb = on_output

    if pty is not None:
        try:
            result = _run_with_pty(
                run_script,
                timeout=timeout,
                max_output=max_output,
                on_output=stream_cb,
                cancel=cancel,
            )
        except OSError:
            result = _run_with_pipes(
                run_script,
                timeout=timeout,
                max_output=max_output,
                on_output=stream_cb,
                cancel=cancel,
            )
    else:
        result = _run_with_pipes(
            run_script,
            timeout=timeout,
            max_output=max_output,
            on_output=stream_cb,
            cancel=cancel,
        )

    if tracker is not None:
        return tracker.finalize(result, fallback_cwd=cwd)
    return result


def default_cwd() -> str:
    home = os.path.expanduser("~")
    if home and os.path.isdir(home):
        return home
    return os.getcwd()


def _wrap_with_cwd(
    command: str,
    cwd: str,
    on_output: OutputCallback | None,
) -> tuple[str, "_CwdOutputFilter"]:
    tracker = _CwdOutputFilter(on_output)
    # Run in one shell so cd inside the user command persists until we capture pwd.
    script = (
        f"cd {shlex.quote(cwd)} || exit 121\n"
        f"{command}\n"
        f"__ohs_ec=$?\n"
        f"printf '%s%s\\n' '{_CWD_MARKER}' \"$(pwd)\"\n"
        f"exit $__ohs_ec\n"
    )
    return script, tracker


class _CwdOutputFilter:
    """Strip the trailing cwd marker from live output and capture the new path."""

    def __init__(self, on_output: OutputCallback | None) -> None:
        self._on_output = on_output
        self._buf = ""
        self._visible: list[str] = []
        self.cwd: str | None = None

    def on_output(self, stream: str, text: str) -> None:
        if stream != "stdout":
            if self._on_output is not None:
                self._on_output(stream, text)
            return
        self._buf += text
        while "\n" in self._buf:
            line, self._buf = self._buf.split("\n", 1)
            if line.startswith(_CWD_MARKER):
                self.cwd = line[len(_CWD_MARKER) :] or self.cwd
                continue
            piece = line + "\n"
            self._visible.append(piece)
            if self._on_output is not None:
                self._on_output("stdout", piece)
        # Hold back a partial line that could be the start of the marker.
        if self._buf and not _CWD_MARKER.startswith(self._buf) and _CWD_MARKER not in self._buf:
            piece = self._buf
            self._buf = ""
            self._visible.append(piece)
            if self._on_output is not None:
                self._on_output("stdout", piece)

    def finalize(self, result: CommandResult, *, fallback_cwd: str | None) -> CommandResult:
        visible, found_cwd = _split_cwd_marker(result.stdout)
        if found_cwd:
            self.cwd = found_cwd

        if result.exit_code == 121 and not result.cancelled and not result.timed_out:
            stderr = result.stderr.rstrip()
            note = f"No such directory (cwd): {fallback_cwd}"
            stderr = f"{stderr}\n{note}".strip() if stderr else note
            return CommandResult(
                visible,
                stderr,
                121,
                False,
                False,
                fallback_cwd,
            )

        return CommandResult(
            visible,
            result.stderr,
            result.exit_code,
            result.timed_out,
            result.cancelled,
            self.cwd or fallback_cwd,
        )


def _split_cwd_marker(text: str) -> tuple[str, str | None]:
    if _CWD_MARKER not in text:
        return text, None
    lines = text.splitlines(keepends=True)
    kept: list[str] = []
    found: str | None = None
    for line in lines:
        bare = line.rstrip("\r\n")
        if bare.startswith(_CWD_MARKER):
            found = bare[len(_CWD_MARKER) :]
            continue
        kept.append(line)
    return "".join(kept), found


def _run_with_pty(
    command: str,
    *,
    timeout: float,
    max_output: int,
    on_output: OutputCallback | None,
    cancel: CommandCancel,
) -> CommandResult:
    master_fd, slave_fd = pty.openpty()
    try:
        proc = subprocess.Popen(
            command,
            shell=True,
            stdin=slave_fd,
            stdout=slave_fd,
            stderr=slave_fd,
            close_fds=True,
            start_new_session=True,
        )
    except OSError:
        os.close(master_fd)
        os.close(slave_fd)
        raise
    os.close(slave_fd)
    cancel.bind(proc, tty_fd=master_fd)

    stdout_parts: list[str] = []
    stdout_len = 0
    timed_out = False
    cancelled = False
    deadline = time.monotonic() + timeout
    sigint_sent_at: float | None = None

    def _append(chunk: str) -> None:
        nonlocal stdout_len
        if not chunk:
            return
        room = max_output - stdout_len
        if room <= 0:
            return
        piece = chunk if len(chunk) <= room else chunk[:room]
        stdout_parts.append(piece)
        stdout_len += len(piece)
        if on_output is not None:
            on_output("stdout", piece)

    try:
        while True:
            if cancel.is_set():
                cancelled = True
                now = time.monotonic()
                if sigint_sent_at is None:
                    try:
                        os.write(master_fd, b"\x03")
                    except OSError:
                        pass
                    _signal_process_group(proc, signal.SIGINT)
                    sigint_sent_at = now
                elif now - sigint_sent_at > 0.5:
                    _kill_process_group(proc)
                    break

            remaining = deadline - time.monotonic()
            if remaining <= 0:
                timed_out = True
                _kill_process_group(proc)
                break

            ready, _, _ = select.select(
                [master_fd], [], [], min(0.2, max(remaining, 0.01))
            )
            if ready:
                try:
                    data = os.read(master_fd, 4096)
                except OSError:
                    data = b""
                if not data:
                    break
                _append(_decode_chunk(data))
            elif proc.poll() is not None:
                while True:
                    ready, _, _ = select.select([master_fd], [], [], 0)
                    if not ready:
                        break
                    try:
                        data = os.read(master_fd, 4096)
                    except OSError:
                        data = b""
                    if not data:
                        break
                    _append(_decode_chunk(data))
                break

        if timed_out or cancelled:
            drain_deadline = time.monotonic() + 0.4
            while time.monotonic() < drain_deadline:
                ready, _, _ = select.select([master_fd], [], [], 0.1)
                if not ready:
                    if proc.poll() is not None:
                        break
                    continue
                try:
                    data = os.read(master_fd, 4096)
                except OSError:
                    break
                if not data:
                    break
                _append(_decode_chunk(data))
            try:
                proc.wait(timeout=1.5)
            except subprocess.TimeoutExpired:
                _kill_process_group(proc)
                proc.wait(timeout=2)
            exit_code = 130 if cancelled and not timed_out else 124
        else:
            exit_code = proc.wait(timeout=max(deadline - time.monotonic(), 0.1))
            if cancel.is_set():
                cancelled = True
                exit_code = 130
    except Exception:
        _kill_process_group(proc)
        raise
    finally:
        cancel.clear_tty()
        try:
            os.close(master_fd)
        except OSError:
            pass

    stdout = "".join(stdout_parts)
    if timed_out:
        timeout_note = f"Command timed out after {timeout:.0f}s"
        if stdout:
            stdout = _truncate(f"{stdout.rstrip()}\n{timeout_note}", max_output)
            return CommandResult(
                _maybe_mark_truncated(stdout, stdout_len, max_output),
                "",
                124,
                True,
                False,
            )
        return CommandResult("", timeout_note, 124, True, False)

    if cancelled:
        note = "Stopped (Ctrl+C)"
        if stdout:
            stdout = _truncate(f"{stdout.rstrip()}\n{note}", max_output)
            return CommandResult(
                _maybe_mark_truncated(stdout, stdout_len, max_output),
                "",
                130,
                False,
                True,
            )
        return CommandResult("", note, 130, False, True)

    return CommandResult(
        _maybe_mark_truncated(stdout, stdout_len, max_output),
        "",
        int(exit_code),
        False,
        False,
    )


def _run_with_pipes(
    command: str,
    *,
    timeout: float,
    max_output: int,
    on_output: OutputCallback | None,
    cancel: CommandCancel,
) -> CommandResult:
    try:
        proc = subprocess.Popen(
            command,
            shell=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
            start_new_session=True,
        )
    except OSError as exc:
        return CommandResult("", str(exc), 1, False)

    cancel.bind(proc)
    assert proc.stdout is not None
    assert proc.stderr is not None

    stdout_parts: list[str] = []
    stderr_parts: list[str] = []
    stdout_len = 0
    stderr_len = 0
    timed_out = False
    cancelled = False
    deadline = time.monotonic() + timeout
    sigint_sent_at: float | None = None
    streams = {proc.stdout: "stdout", proc.stderr: "stderr"}

    def _append(stream: str, chunk: str) -> None:
        nonlocal stdout_len, stderr_len
        if not chunk:
            return
        if stream == "stdout":
            room = max_output - stdout_len
            if room <= 0:
                return
            piece = chunk if len(chunk) <= room else chunk[:room]
            stdout_parts.append(piece)
            stdout_len += len(piece)
        else:
            room = max_output - stderr_len
            if room <= 0:
                return
            piece = chunk if len(chunk) <= room else chunk[:room]
            stderr_parts.append(piece)
            stderr_len += len(piece)
        if on_output is not None:
            on_output(stream, piece)

    try:
        while streams:
            if cancel.is_set():
                cancelled = True
                now = time.monotonic()
                if sigint_sent_at is None:
                    _signal_process_group(proc, signal.SIGINT)
                    sigint_sent_at = now
                elif now - sigint_sent_at > 0.5:
                    _kill_process_group(proc)
                    break

            remaining = deadline - time.monotonic()
            if remaining <= 0:
                timed_out = True
                _kill_process_group(proc)
                break

            ready, _, _ = select.select(
                list(streams), [], [], min(0.25, max(remaining, 0.01))
            )
            if not ready:
                if proc.poll() is not None:
                    for fh, name in list(streams.items()):
                        data = fh.read()
                        if data:
                            _append(name, data)
                        streams.pop(fh, None)
                    break
                continue

            for fh in ready:
                data = fh.read(4096)
                if data:
                    _append(streams[fh], data)
                else:
                    streams.pop(fh, None)

        if timed_out or cancelled:
            for fh, name in list(streams.items()):
                try:
                    data = fh.read()
                except ValueError:
                    data = ""
                if data:
                    _append(name, data)
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                _kill_process_group(proc)
                proc.wait(timeout=2)
            exit_code = 130 if cancelled and not timed_out else 124
        else:
            exit_code = proc.wait(timeout=max(deadline - time.monotonic(), 0.1))
            if cancel.is_set():
                cancelled = True
                exit_code = 130
    except Exception:
        _kill_process_group(proc)
        raise
    finally:
        proc.stdout.close()
        proc.stderr.close()

    stdout = "".join(stdout_parts)
    stderr = "".join(stderr_parts)
    if timed_out:
        timeout_note = f"Command timed out after {timeout:.0f}s"
        if not stderr:
            stderr = timeout_note
        elif timeout_note not in stderr:
            stderr = _truncate(f"{stderr.rstrip()}\n{timeout_note}", max_output)
        return CommandResult(
            _maybe_mark_truncated(stdout, stdout_len, max_output),
            _maybe_mark_truncated(stderr, len(stderr), max_output),
            124,
            True,
            False,
        )

    if cancelled:
        note = "Stopped (Ctrl+C)"
        if not stderr:
            stderr = note
        elif note not in stderr:
            stderr = _truncate(f"{stderr.rstrip()}\n{note}", max_output)
        return CommandResult(
            _maybe_mark_truncated(stdout, stdout_len, max_output),
            _maybe_mark_truncated(stderr, len(stderr), max_output),
            130,
            False,
            True,
        )

    return CommandResult(
        _maybe_mark_truncated(stdout, stdout_len, max_output),
        _maybe_mark_truncated(stderr, stderr_len, max_output),
        int(exit_code),
        False,
        False,
    )


def _decode_chunk(data: bytes) -> str:
    # PTYs often emit CRLF; normalize for Telegram display.
    return data.decode("utf-8", errors="replace").replace("\r\n", "\n").replace("\r", "\n")


def _signal_process_group(proc: subprocess.Popen, sig: int) -> None:
    try:
        os.killpg(proc.pid, sig)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            proc.send_signal(sig)
        except OSError:
            pass


def _kill_process_group(proc: subprocess.Popen) -> None:
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            proc.kill()
        except OSError:
            pass


def _maybe_mark_truncated(text: str, collected_len: int, max_output: int) -> str:
    if collected_len < max_output:
        return text
    if "truncated" in text[-60:]:
        return text
    return text[: max_output - 40] + f"\n... (truncated, {collected_len}+ chars total)"


def _truncate(text: str, max_output: int) -> str:
    if len(text) <= max_output:
        return text
    return text[: max_output - 40] + f"\n... (truncated, {len(text)} chars total)"


def format_command_progress(
    *,
    label: str,
    command: str,
    stdout: str,
    stderr: str,
    elapsed_s: float,
    cwd: str | None = None,
) -> str:
    header = f"🖥 {label}"
    if cwd:
        header = f"{header}\n📂 {cwd}"
    lines = [
        header,
        f"$ {command}",
        "",
        f"⏳ Running… ({elapsed_s:.0f}s)",
        "",
    ]
    out = stdout.rstrip()
    err = stderr.rstrip()
    if out:
        lines.append(out)
    if err:
        if out:
            lines.append("")
        lines.append(err)
    if not out and not err:
        lines.append("(waiting for output…)")
    text = "\n".join(lines)
    if len(text) > 4096:
        header_lines = 6 if cwd else 5
        header = "\n".join(lines[:header_lines]) + "\n"
        body = (out + ("\n\n" + err if err else "")).rstrip()
        marker = "... (earlier output truncated)\n"
        body_budget = 4050 - len(header) - len(marker)
        if len(body) > body_budget > 0:
            body = marker + body[-body_budget:]
        return (header + body)[:4096]
    return text


def _is_cat_command(command: str) -> bool:
    tokens = _leading_command_tokens(command.strip())
    if not tokens:
        return False
    return os.path.basename(tokens[0].rstrip("/")) == "cat"


def format_command_result(
    *,
    label: str,
    command: str,
    result: CommandResult,
    cwd: str | None = None,
) -> tuple[str, str | None]:
    """Return (message_text, parse_mode). parse_mode is HTML for successful cat."""
    path = cwd or result.cwd
    use_code = (
        _is_cat_command(command)
        and result.exit_code == 0
        and not result.cancelled
        and not result.timed_out
        and bool(result.stdout.strip())
    )

    if use_code:
        lines = [html.escape(f"🖥 {label}")]
        if path:
            lines.append(html.escape(f"📂 {path}"))
        lines.append(html.escape(f"$ {command}"))
        lines.append("")
        # Telegram HTML <pre> renders as a monospace code block.
        body = html.escape(result.stdout.rstrip("\n"))
        lines.append(f"<pre>{body}</pre>")
        if result.stderr.strip():
            lines.append("")
            lines.append(html.escape(result.stderr.rstrip()))
        lines.append("")
        lines.append(html.escape(f"Exit code: {result.exit_code}"))
        text = "\n".join(lines)
        if len(text) > 4096:
            # Keep header + trimmed pre body.
            overhead = len(text) - len(body)
            keep = max(200, 4000 - overhead - len("</pre>"))
            body = body[:keep] + "\n..."
            text = "\n".join(lines[:-3] + [f"<pre>{body}</pre>", "", lines[-1]])
            text = text[:4096]
        return text, "HTML"

    lines = [f"🖥 {label}"]
    if path:
        lines.append(f"📂 {path}")
    lines.extend([f"$ {command}", ""])
    if result.cancelled:
        lines.append("⏹ Stopped (Ctrl+C)")
        lines.append("")
    elif result.timed_out:
        lines.append("⏱ Timed out")
        lines.append("")

    stdout = result.stdout.rstrip()
    stderr = result.stderr.rstrip()
    if stdout:
        lines.append(stdout)
    if stderr:
        if stdout:
            lines.append("")
        lines.append(stderr)
    if not stdout and not stderr:
        lines.append("(no output)")

    lines.append("")
    lines.append(f"Exit code: {result.exit_code}")
    text = "\n".join(lines)
    if len(text) > 4096:
        return text[:4050] + "\n... (message truncated)", None
    return text, None
