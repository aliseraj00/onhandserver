import subprocess
from dataclasses import dataclass


@dataclass(frozen=True)
class CommandResult:
    stdout: str
    stderr: str
    exit_code: int
    timed_out: bool


def run_command(
    command: str,
    *,
    timeout: float = 30.0,
    max_output: int = 3500,
) -> CommandResult:
    command = command.strip()
    if not command:
        return CommandResult("", "Empty command", 1, False)

    try:
        completed = subprocess.run(
            command,
            shell=True,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        stdout = _truncate(completed.stdout or "", max_output)
        stderr = _truncate(completed.stderr or "", max_output)
        return CommandResult(stdout, stderr, completed.returncode, False)
    except subprocess.TimeoutExpired as exc:
        stdout = _truncate((exc.stdout or "") if isinstance(exc.stdout, str) else "", max_output)
        stderr = _truncate((exc.stderr or "") if isinstance(exc.stderr, str) else "", max_output)
        if not stderr:
            stderr = f"Command timed out after {timeout:.0f}s"
        return CommandResult(stdout, stderr, 124, True)


def _truncate(text: str, max_output: int) -> str:
    if len(text) <= max_output:
        return text
    return text[: max_output - 40] + f"\n... (truncated, {len(text)} chars total)"


def format_command_result(
    *,
    label: str,
    command: str,
    result: CommandResult,
) -> str:
    lines = [f"🖥 {label}", f"$ {command}", ""]
    if result.timed_out:
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
        return text[:4050] + "\n... (message truncated)"
    return text
