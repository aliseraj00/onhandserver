import os
import platform
import socket
import time
from dataclasses import dataclass

import psutil


@dataclass
class ResourceSnapshot:
    cpu_percent_per_core: list[float]
    cpu_percent_avg: float
    ram_total_gb: float
    ram_used_gb: float
    ram_percent: float
    swap_used_gb: float
    swap_total_gb: float
    swap_percent: float
    disk_total_gb: float
    disk_used_gb: float
    disk_percent: float
    disk_path: str
    hostname: str
    load_avg: tuple[float, float, float] | None
    uptime_seconds: float


def sample_resources(disk_path: str, cpu_interval: float = 1.0) -> ResourceSnapshot:
    per_core = psutil.cpu_percent(interval=cpu_interval, percpu=True)
    avg_cpu = sum(per_core) / len(per_core) if per_core else 0.0

    memory = psutil.virtual_memory()
    swap = psutil.swap_memory()
    disk = psutil.disk_usage(disk_path)
    load_avg = os.getloadavg() if hasattr(os, "getloadavg") else None

    return ResourceSnapshot(
        cpu_percent_per_core=per_core,
        cpu_percent_avg=avg_cpu,
        ram_total_gb=memory.total / (1024**3),
        ram_used_gb=memory.used / (1024**3),
        ram_percent=memory.percent,
        swap_used_gb=swap.used / (1024**3),
        swap_total_gb=swap.total / (1024**3),
        swap_percent=swap.percent,
        disk_total_gb=disk.total / (1024**3),
        disk_used_gb=disk.used / (1024**3),
        disk_percent=disk.percent,
        disk_path=disk_path,
        hostname=socket.gethostname(),
        load_avg=load_avg,
        uptime_seconds=time.time() - psutil.boot_time(),
    )


def _format_uptime(seconds: float) -> str:
    days, rem = divmod(int(seconds), 86400)
    hours, rem = divmod(rem, 3600)
    minutes, _ = divmod(rem, 60)
    parts = []
    if days:
        parts.append(f"{days}d")
    if hours or days:
        parts.append(f"{hours}h")
    parts.append(f"{minutes}m")
    return " ".join(parts)


def format_status(snapshot: ResourceSnapshot) -> str:
    core_lines = ", ".join(f"{value:.0f}%" for value in snapshot.cpu_percent_per_core)
    lines = [
        f"🖥 Server: {snapshot.hostname} ({platform.system()})",
        f"Uptime: {_format_uptime(snapshot.uptime_seconds)}",
        "",
        f"CPU average: {snapshot.cpu_percent_avg:.1f}%",
        f"CPU per core: {core_lines}",
    ]
    if snapshot.load_avg is not None:
        load_1, load_5, load_15 = snapshot.load_avg
        lines.append(f"Load avg: {load_1:.2f} {load_5:.2f} {load_15:.2f}")
    lines.extend(
        [
            "",
            f"RAM: {snapshot.ram_used_gb:.2f} / {snapshot.ram_total_gb:.2f} GB "
            f"({snapshot.ram_percent:.1f}%)",
        ]
    )
    if snapshot.swap_total_gb > 0:
        lines.append(
            f"Swap: {snapshot.swap_used_gb:.2f} / {snapshot.swap_total_gb:.2f} GB "
            f"({snapshot.swap_percent:.1f}%)"
        )
    lines.append(
        f"Disk ({snapshot.disk_path}): {snapshot.disk_used_gb:.2f} / "
        f"{snapshot.disk_total_gb:.2f} GB ({snapshot.disk_percent:.1f}%)"
    )
    return "\n".join(lines)
