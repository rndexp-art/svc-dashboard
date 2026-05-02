"""GitHub API helpers — deploy history + submodule SHAs.

We use a `GITHUB_PAT` (passed in as `DASHBOARD_GH_PAT` so we don't shadow
the reserved namespace) to talk to api.github.com. If unset, every call
returns a soft-failure shape and the routes show a "GH PAT not configured"
hint instead of crashing.

Cached for 60s so flipping between /services and /vps doesn't burn rate
limit. Cache is process-local; that's fine for a single uvicorn worker.
"""
from __future__ import annotations

import os
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import httpx


GH_API = "https://api.github.com"


def _gh_pat() -> str:
    return os.environ.get("DASHBOARD_GH_PAT") or os.environ.get("GITHUB_PAT") or ""


def _gh_repo() -> str:
    return os.environ.get("DASHBOARD_GH_REPO", "rndexp-art/rndexpart")


def _gh_org() -> str:
    return _gh_repo().split("/", 1)[0]


def gh_configured() -> bool:
    return bool(_gh_pat())


# --- minimal cache ---------------------------------------------------------

_CACHE: dict[str, tuple[float, Any]] = {}
_CACHE_TTL_S = 60.0


def _cached(key: str, fn):
    now = time.time()
    if key in _CACHE:
        ts, val = _CACHE[key]
        if now - ts < _CACHE_TTL_S:
            return val
    val = fn()
    _CACHE[key] = (now, val)
    return val


def invalidate_cache() -> None:
    _CACHE.clear()


# --- HTTP ------------------------------------------------------------------

def _client() -> httpx.Client:
    return httpx.Client(
        base_url=GH_API,
        headers={
            "Authorization": f"Bearer {_gh_pat()}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
        timeout=httpx.Timeout(connect=2.0, read=8.0, write=4.0, pool=2.0),
    )


# --- workflow runs ---------------------------------------------------------

@dataclass(frozen=True)
class WorkflowRun:
    id: int
    name: str
    status: str          # queued / in_progress / completed
    conclusion: str      # success / failure / cancelled / "" (still running)
    event: str
    head_sha: str
    head_branch: str
    actor: str
    created_at: datetime | None
    updated_at: datetime | None
    html_url: str


def _parse_dt(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


def recent_runs(workflow_filename: str = "deploy-gateway.yml", per_page: int = 10) -> list[WorkflowRun]:
    if not gh_configured():
        return []

    def fetch() -> list[WorkflowRun]:
        with _client() as c:
            r = c.get(
                f"/repos/{_gh_repo()}/actions/workflows/{workflow_filename}/runs",
                params={"per_page": per_page},
            )
            r.raise_for_status()
            data = r.json()
        runs: list[WorkflowRun] = []
        for raw in data.get("workflow_runs", []):
            runs.append(WorkflowRun(
                id=raw.get("id", 0),
                name=raw.get("name", ""),
                status=raw.get("status", ""),
                conclusion=raw.get("conclusion") or "",
                event=raw.get("event", ""),
                head_sha=raw.get("head_sha", "")[:7],
                head_branch=raw.get("head_branch", ""),
                actor=(raw.get("actor") or {}).get("login", ""),
                created_at=_parse_dt(raw.get("created_at")),
                updated_at=_parse_dt(raw.get("updated_at")),
                html_url=raw.get("html_url", ""),
            ))
        return runs

    return _cached(f"runs:{workflow_filename}:{per_page}", fetch)


def all_repo_runs(repo: str, per_page: int = 5) -> list[WorkflowRun]:
    """Recent runs across all workflows in `repo` (e.g. `rndexp-art/svc-auth`)."""
    if not gh_configured():
        return []

    def fetch() -> list[WorkflowRun]:
        with _client() as c:
            r = c.get(f"/repos/{repo}/actions/runs", params={"per_page": per_page})
            r.raise_for_status()
            data = r.json()
        runs: list[WorkflowRun] = []
        for raw in data.get("workflow_runs", []):
            runs.append(WorkflowRun(
                id=raw.get("id", 0),
                name=raw.get("name", ""),
                status=raw.get("status", ""),
                conclusion=raw.get("conclusion") or "",
                event=raw.get("event", ""),
                head_sha=raw.get("head_sha", "")[:7],
                head_branch=raw.get("head_branch", ""),
                actor=(raw.get("actor") or {}).get("login", ""),
                created_at=_parse_dt(raw.get("created_at")),
                updated_at=_parse_dt(raw.get("updated_at")),
                html_url=raw.get("html_url", ""),
            ))
        return runs

    return _cached(f"runs-all:{repo}:{per_page}", fetch)


# --- submodule pins --------------------------------------------------------

@dataclass(frozen=True)
class SubmodulePin:
    path: str            # e.g. "services/auth"
    sha: str             # the pinned commit SHA on production
    repo: str | None     # GitHub repo slug ("rndexp-art/svc-auth"), if discoverable
    short_sha: str
    commit_html_url: str | None
    commit_message: str | None


def submodule_pins(branch: str = "production") -> list[SubmodulePin]:
    """One row per submodule under `services/`, pinned at the given branch.

    The GitHub API returns submodule entries inline when you list a directory
    via the contents endpoint. Each entry's `submodule_git_url` gives us the
    upstream repo, and the `sha` field is the pinned commit.
    """
    if not gh_configured():
        return []

    def fetch() -> list[SubmodulePin]:
        with _client() as c:
            r = c.get(
                f"/repos/{_gh_repo()}/contents/services",
                params={"ref": branch},
            )
            r.raise_for_status()
            entries = r.json()
            pins: list[SubmodulePin] = []
            for e in entries:
                if e.get("type") != "submodule":
                    continue
                sha = e.get("sha", "")
                git_url = e.get("submodule_git_url") or ""
                repo = _gh_repo_from_url(git_url)
                msg, html = (None, None)
                if repo and sha:
                    cm = _commit_meta(c, repo, sha)
                    msg, html = cm
                pins.append(SubmodulePin(
                    path=e.get("path", ""),
                    sha=sha,
                    short_sha=sha[:7],
                    repo=repo,
                    commit_message=msg,
                    commit_html_url=html,
                ))
        pins.sort(key=lambda p: p.path)
        return pins

    return _cached(f"pins:{branch}", fetch)


def _gh_repo_from_url(url: str) -> str | None:
    """Pull the org/repo slug out of a submodule URL, e.g.
       https://github.com/rndexp-art/svc-auth.git → rndexp-art/svc-auth
    """
    u = url.strip()
    for prefix in ("https://github.com/", "git@github.com:"):
        if u.startswith(prefix):
            u = u[len(prefix):]
            break
    else:
        return None
    if u.endswith(".git"):
        u = u[:-4]
    parts = u.split("/")
    if len(parts) != 2:
        return None
    return f"{parts[0]}/{parts[1]}"


def _commit_meta(c: httpx.Client, repo: str, sha: str) -> tuple[str | None, str | None]:
    try:
        r = c.get(f"/repos/{repo}/commits/{sha}")
        if r.status_code != 200:
            return None, None
        data = r.json()
        return (
            (data.get("commit") or {}).get("message", "").splitlines()[0] if data else None,
            data.get("html_url"),
        )
    except (httpx.HTTPError, ValueError):
        return None, None
