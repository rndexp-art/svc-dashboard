"""Pull-on-page-load VPS metrics.

We read the host's /proc and /sys via bind mounts at /host/proc and
/host/sys (set up in compose.fragment.yml). No SSH key, no extra agent —
the dashboard container is on the VPS already, this is just stat()ing
files inside its own filesystem.

CPU is sampled twice with a short sleep so we can compute a real busy %.
Each call costs ~150ms; that's acceptable on a page that's hit by hand.

If /host/proc isn't mounted (local dev on macOS), every probe returns a
soft-failure shape with `available=False` so the template can render a
muted placeholder rather than a 500.
"""
from __future__ import annotations

import os
import shutil
import time
from dataclasses import dataclass
from pathlib import Path


# --- where we read host metrics from ----------------------------------------

def _proc_root() -> Path:
    return Path(os.environ.get("DASHBOARD_HOST_PROC", "/host/proc"))


def _sys_root() -> Path:
    return Path(os.environ.get("DASHBOARD_HOST_SYS", "/host/sys"))


def host_metrics_available() -> bool:
    """True if /host/proc/stat is readable. False on macOS dev or when
    the bind mount is missing."""
    return (_proc_root() / "stat").is_file()


# --- CPU --------------------------------------------------------------------

@dataclass(frozen=True)
class CpuSample:
    user: int
    nice: int
    system: int
    idle: int
    iowait: int
    irq: int
    softirq: int
    steal: int

    @property
    def total(self) -> int:
        return self.user + self.nice + self.system + self.idle + self.iowait + self.irq + self.softirq + self.steal

    @property
    def busy(self) -> int:
        return self.total - self.idle - self.iowait


def _read_cpu_sample() -> CpuSample:
    line = (_proc_root() / "stat").read_text().splitlines()[0]
    # /proc/stat: "cpu  user nice system idle iowait irq softirq steal guest guest_nice"
    parts = [int(x) for x in line.split()[1:9]]
    return CpuSample(*parts)


@dataclass(frozen=True)
class CpuMetrics:
    available: bool
    cores: int = 0
    busy_pct: float = 0.0
    user_pct: float = 0.0
    system_pct: float = 0.0
    iowait_pct: float = 0.0
    load_1m: float = 0.0
    load_5m: float = 0.0
    load_15m: float = 0.0


def cpu(sample_window_s: float = 0.15) -> CpuMetrics:
    if not host_metrics_available():
        return CpuMetrics(available=False)

    a = _read_cpu_sample()
    time.sleep(sample_window_s)
    b = _read_cpu_sample()
    cores = os.cpu_count() or 1
    try:
        load = (_proc_root() / "loadavg").read_text().split()[:3]
        load_1m, load_5m, load_15m = (float(x) for x in load)
    except (FileNotFoundError, ValueError):
        load_1m = load_5m = load_15m = 0.0

    dtotal = b.total - a.total
    if dtotal <= 0:
        # Identical samples (rare in real life, common in tests with a fake
        # /proc): report 0% busy but still surface the load average.
        return CpuMetrics(
            available=True, cores=cores,
            load_1m=load_1m, load_5m=load_5m, load_15m=load_15m,
        )

    def pct(numer: int) -> float:
        return round(100.0 * numer / dtotal, 1)

    return CpuMetrics(
        available=True,
        cores=cores,
        busy_pct=pct(b.busy - a.busy),
        user_pct=pct((b.user + b.nice) - (a.user + a.nice)),
        system_pct=pct(b.system - a.system),
        iowait_pct=pct(b.iowait - a.iowait),
        load_1m=load_1m,
        load_5m=load_5m,
        load_15m=load_15m,
    )


# --- memory -----------------------------------------------------------------

@dataclass(frozen=True)
class MemoryMetrics:
    available: bool
    total_mb: int = 0
    used_mb: int = 0
    avail_mb: int = 0
    cached_mb: int = 0
    used_pct: float = 0.0
    swap_total_mb: int = 0
    swap_used_mb: int = 0


def _meminfo() -> dict[str, int]:
    out: dict[str, int] = {}
    for line in (_proc_root() / "meminfo").read_text().splitlines():
        # "MemTotal:       16384492 kB"
        if ":" not in line:
            continue
        k, _, v = line.partition(":")
        v = v.strip()
        amount, _, unit = v.partition(" ")
        try:
            n = int(amount)
        except ValueError:
            continue
        if unit.lower() == "kb":
            out[k.strip()] = n  # store as kB
    return out


def memory() -> MemoryMetrics:
    if not host_metrics_available():
        return MemoryMetrics(available=False)
    m = _meminfo()
    total_kb = m.get("MemTotal", 0)
    avail_kb = m.get("MemAvailable", 0)
    cached_kb = m.get("Cached", 0)
    used_kb = total_kb - avail_kb
    used_pct = round(100.0 * used_kb / total_kb, 1) if total_kb else 0.0
    return MemoryMetrics(
        available=True,
        total_mb=total_kb // 1024,
        used_mb=used_kb // 1024,
        avail_mb=avail_kb // 1024,
        cached_mb=cached_kb // 1024,
        used_pct=used_pct,
        swap_total_mb=m.get("SwapTotal", 0) // 1024,
        swap_used_mb=(m.get("SwapTotal", 0) - m.get("SwapFree", 0)) // 1024,
    )


# --- disk -------------------------------------------------------------------

@dataclass(frozen=True)
class DiskUsage:
    mount: str
    fs: str
    total_gb: float
    used_gb: float
    free_gb: float
    used_pct: float


@dataclass(frozen=True)
class DiskMetrics:
    available: bool
    mounts: list[DiskUsage]


# Skip these — they're virtual or container-internal and would clutter the UI.
_SKIP_FS_TYPES = {"tmpfs", "devtmpfs", "sysfs", "proc", "cgroup", "cgroup2",
                  "overlay", "fuse.gvfsd-fuse", "binfmt_misc", "mqueue",
                  "fusectl", "pstore", "bpf", "tracefs", "debugfs", "configfs",
                  "ramfs", "autofs", "rpc_pipefs", "nsfs", "selinuxfs",
                  "fuse.lxcfs", "squashfs"}
_SKIP_PREFIXES = ("/snap/", "/var/lib/docker/")


def _is_interesting_mount(mount: str, fs_type: str) -> bool:
    if fs_type in _SKIP_FS_TYPES:
        return False
    if any(mount.startswith(p) for p in _SKIP_PREFIXES):
        return False
    if mount in ("", "/host/proc", "/host/sys"):
        return False
    return True


def disk() -> DiskMetrics:
    if not host_metrics_available():
        return DiskMetrics(available=False, mounts=[])
    mounts: list[DiskUsage] = []
    seen: set[str] = set()
    # /host/proc/mounts has every mount point on the host — but the paths
    # are host paths, not paths inside our container. We can't statvfs("/")
    # and get the host root either; we need to read sizes from the host's
    # POV. /host/proc/<pid>/mountinfo would give us that with the device,
    # but a simpler approach: parse /host/proc/mounts and stat the *device
    # node* via /host/proc/diskstats? That doesn't give us free space.
    #
    # Pragmatic answer: the container's *own* / is the host's overlay; the
    # bind mount of /host/proc/mounts plus statvfs of paths visible inside
    # the container won't reflect host paths. We compromise by reporting
    # disk usage for whatever the dashboard *can* statvfs — that's enough
    # for the "VPS getting full?" question. For exact host-mount sizes the
    # operator can SSH; we're a dashboard, not a replacement for `df`.
    for line in (_proc_root() / "mounts").read_text().splitlines():
        parts = line.split()
        if len(parts) < 3:
            continue
        device, mount, fs_type = parts[0], parts[1], parts[2]
        if not _is_interesting_mount(mount, fs_type):
            continue
        # Dedupe on device — bind-mounts of the same FS show up many times.
        if device in seen:
            continue
        seen.add(device)
        try:
            usage = shutil.disk_usage(mount)
        except (FileNotFoundError, PermissionError):
            continue
        if usage.total == 0:
            continue
        gb = lambda b: round(b / (1024**3), 2)
        mounts.append(DiskUsage(
            mount=mount,
            fs=fs_type,
            total_gb=gb(usage.total),
            used_gb=gb(usage.used),
            free_gb=gb(usage.free),
            used_pct=round(100.0 * usage.used / usage.total, 1),
        ))
    mounts.sort(key=lambda d: d.total_gb, reverse=True)
    return DiskMetrics(available=True, mounts=mounts[:10])


# --- network ---------------------------------------------------------------

@dataclass(frozen=True)
class InterfaceCounters:
    name: str
    rx_mb: float
    tx_mb: float
    rx_packets: int
    tx_packets: int
    rx_errors: int
    tx_errors: int


@dataclass(frozen=True)
class NetworkMetrics:
    available: bool
    interfaces: list[InterfaceCounters]
    tcp_listening: int = 0
    tcp_established: int = 0


def _iter_net_dev() -> list[InterfaceCounters]:
    """/proc/net/dev format:
        Inter-|   Receive                                                |  Transmit
         face |bytes    packets errs drop fifo frame compressed multicast|bytes ...
    """
    out: list[InterfaceCounters] = []
    text = (_proc_root() / "net" / "dev").read_text()
    for line in text.splitlines()[2:]:
        if ":" not in line:
            continue
        name, _, rest = line.partition(":")
        name = name.strip()
        # We don't care about loopback or docker bridges.
        if name in ("lo",) or name.startswith(("docker", "veth", "br-")):
            continue
        cols = rest.split()
        if len(cols) < 16:
            continue
        rx_b, rx_p, rx_e = int(cols[0]), int(cols[1]), int(cols[2])
        tx_b, tx_p, tx_e = int(cols[8]), int(cols[9]), int(cols[10])
        out.append(InterfaceCounters(
            name=name,
            rx_mb=round(rx_b / (1024**2), 1),
            tx_mb=round(tx_b / (1024**2), 1),
            rx_packets=rx_p,
            tx_packets=tx_p,
            rx_errors=rx_e,
            tx_errors=tx_e,
        ))
    return out


def _tcp_states() -> tuple[int, int]:
    """Count TCP sockets by state from /proc/net/tcp{,6}.

    Status codes: 0A = LISTEN, 01 = ESTABLISHED.
    """
    listening, established = 0, 0
    for fname in ("tcp", "tcp6"):
        path = _proc_root() / "net" / fname
        if not path.is_file():
            continue
        try:
            for line in path.read_text().splitlines()[1:]:
                cols = line.split()
                if len(cols) < 4:
                    continue
                st = cols[3].lower()
                if st == "0a":
                    listening += 1
                elif st == "01":
                    established += 1
        except (PermissionError, OSError):
            pass
    return listening, established


def network() -> NetworkMetrics:
    if not host_metrics_available():
        return NetworkMetrics(available=False, interfaces=[])
    interfaces = _iter_net_dev()
    listening, established = _tcp_states()
    return NetworkMetrics(
        available=True,
        interfaces=interfaces,
        tcp_listening=listening,
        tcp_established=established,
    )


# --- uptime ----------------------------------------------------------------

def uptime_seconds() -> int | None:
    if not host_metrics_available():
        return None
    try:
        secs = float((_proc_root() / "uptime").read_text().split()[0])
        return int(secs)
    except (FileNotFoundError, ValueError):
        return None


def humanize_uptime(seconds: int | None) -> str:
    if seconds is None:
        return "n/a"
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, _ = divmod(rem, 60)
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"
