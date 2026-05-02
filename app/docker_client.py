"""Talk to docker via the local UNIX socket.

We rely on a read-only mount of `/var/run/docker.sock`. The dashboard
needs:

  - a list of every container in the gateway's compose project
    (filter on `com.docker.compose.project=<DASHBOARD_DOCKER_PROJECT>`),
  - point-in-time CPU / memory stats per container (one synchronous
    sample, not the streaming variant),
  - tail of recent log lines per container.

If the socket isn't reachable (no mount, wrong permissions, dev box
without docker) every call raises `DockerUnavailable`; the routes show
a muted placeholder rather than 500ing.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable

import docker
from docker.errors import DockerException, NotFound

log = logging.getLogger("rndexp_dashboard.docker")


class DockerUnavailable(RuntimeError):
    pass


def _project_name() -> str:
    return os.environ.get("DASHBOARD_DOCKER_PROJECT", "rndexpart")


_client: docker.DockerClient | None = None


def _get_client() -> docker.DockerClient:
    """Lazy DockerClient — created on first use, reused afterwards.

    Raises DockerUnavailable on any docker-side failure so callers can
    treat "no docker" as a single error class instead of catching the
    SDK's exception zoo.
    """
    global _client
    if _client is None:
        try:
            _client = docker.DockerClient(
                base_url=os.environ.get("DOCKER_HOST", "unix:///var/run/docker.sock"),
                timeout=5,
            )
            _client.ping()
        except DockerException as e:
            _client = None
            raise DockerUnavailable(str(e)) from e
    return _client


# --- containers -------------------------------------------------------------

@dataclass(frozen=True)
class ContainerInfo:
    name: str          # short name (compose service name, not full container name)
    container_name: str  # full docker name (e.g. rndexpart-auth-1)
    id: str
    image: str
    status: str        # "running" / "exited" / "restarting" / ...
    health: str        # "healthy" / "unhealthy" / "starting" / "" (none)
    started_at: datetime | None
    ports: list[str]   # human-readable "8001/tcp", "443:443/tcp"
    project: str
    is_gateway_owned: bool


def _strip_compose_suffix(name: str, project: str) -> str:
    """rndexpart-auth-1 → auth"""
    prefix = f"{project}-"
    if name.startswith(prefix):
        rest = name[len(prefix):]
        # drop trailing "-1" replica index if present
        if "-" in rest and rest.rsplit("-", 1)[-1].isdigit():
            rest = rest.rsplit("-", 1)[0]
        return rest
    return name


def _format_ports(attrs: dict[str, Any]) -> list[str]:
    network = attrs.get("NetworkSettings", {}) or {}
    ports = network.get("Ports") or {}
    out: list[str] = []
    for proto_port, bindings in sorted(ports.items()):
        if not bindings:
            out.append(proto_port)
            continue
        for b in bindings:
            host = b.get("HostPort", "?")
            ip = b.get("HostIp", "")
            out.append(f"{ip + ':' if ip and ip != '0.0.0.0' else ''}{host}:{proto_port}")
    return out


def _parse_started_at(s: str) -> datetime | None:
    if not s or s.startswith("0001-"):
        return None
    # docker returns RFC3339 nanos; chop to micros and parse.
    s = s.replace("Z", "+00:00")
    if "." in s:
        head, _, tail = s.partition(".")
        # tail looks like "123456789+00:00"
        sign_idx = max(tail.find("+"), tail.find("-"))
        if sign_idx == -1:
            tail = tail[:6]
            s = head + "." + tail
        else:
            tail = tail[:6] + tail[sign_idx:]
            s = head + "." + tail
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        return None


def list_services() -> list[ContainerInfo]:
    """Every container in the gateway's compose project, plus the gateway
    container itself (its compose project label IS the gateway). Returns
    them sorted by service name.
    """
    project = _project_name()
    cli = _get_client()
    containers = cli.containers.list(all=True)
    out: list[ContainerInfo] = []
    for c in containers:
        labels = c.attrs.get("Config", {}).get("Labels", {}) or {}
        c_project = labels.get("com.docker.compose.project", "")
        if c_project and c_project != project:
            continue
        if not c_project:
            # Not part of any compose project (e.g. random one-off). Skip.
            continue
        state = c.attrs.get("State", {}) or {}
        out.append(ContainerInfo(
            name=labels.get("com.docker.compose.service", _strip_compose_suffix(c.name, project)),
            container_name=c.name,
            id=c.short_id,
            image=c.image.tags[0] if c.image and c.image.tags else c.attrs.get("Config", {}).get("Image", ""),
            status=state.get("Status", "unknown"),
            health=(state.get("Health", {}) or {}).get("Status", ""),
            started_at=_parse_started_at(state.get("StartedAt", "")),
            ports=_format_ports(c.attrs),
            project=c_project,
            is_gateway_owned=True,
        ))
    out.sort(key=lambda c: c.name)
    return out


def get_service(name: str) -> ContainerInfo | None:
    for s in list_services():
        if s.name == name:
            return s
    return None


# --- single-shot stats ------------------------------------------------------

@dataclass(frozen=True)
class ContainerStats:
    available: bool
    cpu_pct: float = 0.0
    mem_used_mb: float = 0.0
    mem_limit_mb: float = 0.0
    mem_pct: float = 0.0


def stats(name: str) -> ContainerStats:
    cli = _get_client()
    try:
        c = cli.containers.get(_resolve_container_name(name))
    except NotFound:
        return ContainerStats(available=False)
    try:
        s = c.stats(stream=False, one_shot=True)  # one_shot returns a single sample
    except DockerException:
        return ContainerStats(available=False)
    cpu_pct = _cpu_percent(s)
    mem = s.get("memory_stats", {}) or {}
    used = float(mem.get("usage", 0))
    # Adjust for cache like docker stats does, when available.
    cache = (mem.get("stats") or {}).get("cache", 0)
    used = max(0.0, used - cache)
    limit = float(mem.get("limit", 0))
    mem_pct = round(100.0 * used / limit, 1) if limit else 0.0
    return ContainerStats(
        available=True,
        cpu_pct=round(cpu_pct, 1),
        mem_used_mb=round(used / (1024**2), 1),
        mem_limit_mb=round(limit / (1024**2), 1),
        mem_pct=mem_pct,
    )


def _cpu_percent(s: dict[str, Any]) -> float:
    """Mirrors `docker stats` math:
        delta = cpu_total - precpu_total
        sys_delta = sys_cpu_total - precpu_sys_total
        cpus = online_cpus
        pct = delta / sys_delta * cpus * 100
    """
    cpu = s.get("cpu_stats", {}) or {}
    pre = s.get("precpu_stats", {}) or {}
    cpu_total = (cpu.get("cpu_usage") or {}).get("total_usage", 0)
    pre_total = (pre.get("cpu_usage") or {}).get("total_usage", 0)
    sys_total = cpu.get("system_cpu_usage", 0)
    pre_sys = pre.get("system_cpu_usage", 0)
    cpus = cpu.get("online_cpus", 0) or len((cpu.get("cpu_usage") or {}).get("percpu_usage") or [1])
    delta = cpu_total - pre_total
    sys_delta = sys_total - pre_sys
    if delta <= 0 or sys_delta <= 0:
        return 0.0
    return (delta / sys_delta) * cpus * 100.0


# --- logs -------------------------------------------------------------------

def logs(name: str, tail: int = 200) -> str:
    cli = _get_client()
    try:
        c = cli.containers.get(_resolve_container_name(name))
    except NotFound:
        raise DockerUnavailable(f"container {name!r} not found")
    raw = c.logs(stdout=True, stderr=True, tail=tail, timestamps=True)
    if isinstance(raw, bytes):
        return raw.decode("utf-8", errors="replace")
    return str(raw)


def _resolve_container_name(name: str) -> str:
    """Accept either the bare service name (`auth`) or a full container
    name (`rndexpart-auth-1`). Bare names are matched against the project's
    compose service labels.
    """
    if "-" in name:
        return name
    project = _project_name()
    cli = _get_client()
    for c in cli.containers.list(all=True,
                                 filters={"label": f"com.docker.compose.project={project}"}):
        labels = c.attrs.get("Config", {}).get("Labels", {}) or {}
        if labels.get("com.docker.compose.service") == name:
            return c.name
    return name  # let the caller's .get() raise NotFound


# --- helpers for the template -----------------------------------------------

def humanize_uptime(started_at: datetime | None) -> str:
    if started_at is None:
        return "n/a"
    delta = datetime.now(tz=timezone.utc) - started_at.astimezone(timezone.utc)
    secs = int(delta.total_seconds())
    if secs < 0:
        return "n/a"
    days, rem = divmod(secs, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, _ = divmod(rem, 60)
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"
