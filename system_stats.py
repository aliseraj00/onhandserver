import os
import platform
import socket
import time
from dataclasses import dataclass

import psutil


@dataclass
class ProcessUsage:
    name: str
    pid: int
    value: float


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
    top_ram_processes: list[ProcessUsage]
    top_cpu_processes: list[ProcessUsage]


def _prime_process_cpu() -> list[psutil.Process]:
    tracked: list[psutil.Process] = []
    for proc in psutil.process_iter():
        try:
            proc.cpu_percent(None)
            tracked.append(proc)
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            pass
    return tracked


_SKIP_CPU_NAMES = frozenset({"system idle process", "idle"})


def _top_processes(
    tracked: list[psutil.Process], limit: int = 2
) -> tuple[list[ProcessUsage], list[ProcessUsage]]:
    ram_candidates: list[ProcessUsage] = []
    cpu_candidates: list[ProcessUsage] = []

    for proc in tracked:
        try:
            name = proc.name() or f"pid-{proc.pid}"
            memory_info = proc.memory_info()
            ram_gb = memory_info.rss / (1024**3)
            cpu_percent = proc.cpu_percent(None)

            ram_candidates.append(ProcessUsage(name=name, pid=proc.pid, value=ram_gb))
            if proc.pid != 0 and name.lower() not in _SKIP_CPU_NAMES:
                cpu_candidates.append(
                    ProcessUsage(name=name, pid=proc.pid, value=cpu_percent)
                )
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            pass

    top_ram = sorted(ram_candidates, key=lambda p: p.value, reverse=True)[:limit]
    top_cpu = sorted(cpu_candidates, key=lambda p: p.value, reverse=True)[:limit]
    return top_ram, top_cpu


def sample_resources(disk_path: str, cpu_interval: float = 1.0) -> ResourceSnapshot:
    tracked = _prime_process_cpu()
    per_core = psutil.cpu_percent(interval=cpu_interval, percpu=True)
    avg_cpu = sum(per_core) / len(per_core) if per_core else 0.0
    top_ram, top_cpu = _top_processes(tracked)

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
        top_ram_processes=top_ram,
        top_cpu_processes=top_cpu,
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


def snapshot_to_dict(snapshot: ResourceSnapshot) -> dict:
    return {
        "cpu_percent_per_core": snapshot.cpu_percent_per_core,
        "cpu_percent_avg": snapshot.cpu_percent_avg,
        "ram_total_gb": snapshot.ram_total_gb,
        "ram_used_gb": snapshot.ram_used_gb,
        "ram_percent": snapshot.ram_percent,
        "swap_used_gb": snapshot.swap_used_gb,
        "swap_total_gb": snapshot.swap_total_gb,
        "swap_percent": snapshot.swap_percent,
        "disk_total_gb": snapshot.disk_total_gb,
        "disk_used_gb": snapshot.disk_used_gb,
        "disk_percent": snapshot.disk_percent,
        "disk_path": snapshot.disk_path,
        "hostname": snapshot.hostname,
        "load_avg": list(snapshot.load_avg) if snapshot.load_avg else None,
        "uptime_seconds": snapshot.uptime_seconds,
        "top_ram_processes": [
            {"name": p.name, "pid": p.pid, "value": p.value}
            for p in snapshot.top_ram_processes
        ],
        "top_cpu_processes": [
            {"name": p.name, "pid": p.pid, "value": p.value}
            for p in snapshot.top_cpu_processes
        ],
    }


def snapshot_from_dict(data: dict) -> ResourceSnapshot:
    load_avg = data.get("load_avg")
    return ResourceSnapshot(
        cpu_percent_per_core=[float(v) for v in data["cpu_percent_per_core"]],
        cpu_percent_avg=float(data["cpu_percent_avg"]),
        ram_total_gb=float(data["ram_total_gb"]),
        ram_used_gb=float(data["ram_used_gb"]),
        ram_percent=float(data["ram_percent"]),
        swap_used_gb=float(data["swap_used_gb"]),
        swap_total_gb=float(data["swap_total_gb"]),
        swap_percent=float(data["swap_percent"]),
        disk_total_gb=float(data["disk_total_gb"]),
        disk_used_gb=float(data["disk_used_gb"]),
        disk_percent=float(data["disk_percent"]),
        disk_path=str(data["disk_path"]),
        hostname=str(data["hostname"]),
        load_avg=tuple(load_avg) if load_avg else None,
        uptime_seconds=float(data["uptime_seconds"]),
        top_ram_processes=[
            ProcessUsage(name=p["name"], pid=int(p["pid"]), value=float(p["value"]))
            for p in data.get("top_ram_processes", [])
        ],
        top_cpu_processes=[
            ProcessUsage(name=p["name"], pid=int(p["pid"]), value=float(p["value"]))
            for p in data.get("top_cpu_processes", [])
        ],
    )


def format_status(snapshot: ResourceSnapshot, *, display_name: str | None = None) -> str:
    title = display_name or snapshot.hostname
    core_lines = ", ".join(f"{value:.0f}%" for value in snapshot.cpu_percent_per_core)
    lines = [
        f"🖥 {title} ({snapshot.hostname}, {platform.system()})"
        if display_name
        else f"🖥 Server: {snapshot.hostname} ({platform.system()})",
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

    lines.extend(["", "Top RAM:"])
    if snapshot.top_ram_processes:
        for proc in snapshot.top_ram_processes:
            lines.append(
                f"  {proc.name} ({proc.pid}): {proc.value:.2f} GB"
            )
    else:
        lines.append("  (none)")

    lines.append("")
    lines.append("Top CPU:")
    if snapshot.top_cpu_processes:
        for proc in snapshot.top_cpu_processes:
            lines.append(f"  {proc.name} ({proc.pid}): {proc.value:.1f}%")
    else:
        lines.append("  (none)")

    return "\n".join(lines)


def format_top_processes_block(
    snapshot: ResourceSnapshot, *, kind: str, limit: int = 2
) -> str:
    if kind == "cpu":
        procs = snapshot.top_cpu_processes[:limit]
        if not procs:
            return ""
        lines = ["", "Top CPU apps:"]
        for proc in procs:
            lines.append(f"  • {proc.name} ({proc.pid}): {proc.value:.1f}%")
        return "\n".join(lines)

    if kind == "ram":
        procs = snapshot.top_ram_processes[:limit]
        if not procs:
            return ""
        lines = ["", "Top RAM apps:"]
        for proc in procs:
            lines.append(f"  • {proc.name} ({proc.pid}): {proc.value:.2f} GB")
        return "\n".join(lines)

    return ""
