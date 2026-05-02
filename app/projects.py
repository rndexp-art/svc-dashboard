"""Project lifecycle: create a new svc-X repo, branch the gateway, merge.

Implemented entirely against the GitHub REST API. The dashboard runs in a
container with no git binary and no SSH key — every operation goes via the
PAT we already use for the deploy-history view.

Lifecycle:

  1. POST /projects/create  →  create_service(name, port)
       a. POST /orgs/{org}/repos                     create empty svc-<name>
       b. POST /repos/{org}/svc-<name>/git/blobs     one per scaffold file
       c. POST .../git/trees                         build the tree
       d. POST .../git/commits                       single seed commit
       e. POST .../git/refs                          point main + production at it
     Then on the gateway repo:
       f. GET  .../git/ref/heads/production          base SHA
       g. GET  .../git/trees/{sha}?recursive=1       find .gitmodules, services.yml
       h. mutate those two files in memory + add a submodule tree entry
          (mode 160000, type=commit, sha=<svc seed commit>)
       i. POST .../git/trees + commits + refs        new tree, commit, branch feat/add-<name>

  2. GET  /projects                →  list_pending_branches()
       returns every gateway branch matching feat/add-* with its head SHA,
       last commit message, and an html_url to GitHub.

  3. POST /projects/{name}/merge   →  merge_to_production(name)
       a. POST .../pulls                              feat/add-<name> → production
       b. PUT  .../pulls/{n}/merge                    merge it
       c. POST .../pulls                              production → main
       d. PUT  .../pulls/{n}/merge                    merge it
     Returns both PR URLs so the operator can audit the trail.

Templates live in the gateway repo at templates/service/. We fetch them on
demand via the contents API (cached 5 min) so the dashboard stays a thin
client of the gateway's source of truth.
"""
from __future__ import annotations

import base64
import re
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import httpx
from fastapi import HTTPException

from . import github_client


GATEWAY_TEMPLATE_PATH = "templates/service"
SUBMODULE_BRANCH = "production"  # which branch of the new svc the gateway pin tracks
NAME_RE = re.compile(r"^[a-z][a-z0-9-]{0,30}$")


# --- HTTP plumbing ---------------------------------------------------------

class GhError(HTTPException):
    """Wrap GitHub 4xx/5xx as our own HTTPException with a readable detail."""


def _client() -> httpx.Client:
    if not github_client.gh_configured():
        raise GhError(503, "GitHub PAT not configured (set GITHUB_PAT in the gateway env)")
    return httpx.Client(
        base_url=github_client.GH_API,
        headers={
            "Authorization": f"Bearer {github_client._gh_pat()}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
        timeout=httpx.Timeout(connect=3.0, read=15.0, write=10.0, pool=3.0),
    )


def _raise_for(r: httpx.Response, what: str) -> dict:
    if r.status_code >= 400:
        try:
            msg = r.json().get("message", r.text)
        except ValueError:
            msg = r.text
        raise GhError(r.status_code, f"{what}: {msg}")
    return r.json() if r.content else {}


def _gateway_repo() -> str:
    return github_client._gh_repo()


def _org() -> str:
    return _gateway_repo().split("/", 1)[0]


# --- templates cache -------------------------------------------------------

_TEMPLATE_CACHE: dict[str, tuple[float, list[tuple[str, str]]]] = {}
_TEMPLATE_TTL_S = 300.0


def _fetch_templates(c: httpx.Client) -> list[tuple[str, str]]:
    """Returns [(relpath, raw_text), ...] for every file under
    templates/service/ on the gateway's main branch. Blobs are returned
    decoded as UTF-8 text — the templates are all source files.
    """
    cached = _TEMPLATE_CACHE.get("main")
    if cached and time.time() - cached[0] < _TEMPLATE_TTL_S:
        return cached[1]

    out: list[tuple[str, str]] = []
    _walk_dir(c, _gateway_repo(), GATEWAY_TEMPLATE_PATH, "main", "", out)
    _TEMPLATE_CACHE["main"] = (time.time(), out)
    return out


def _walk_dir(c: httpx.Client, repo: str, base: str, ref: str,
              prefix: str, out: list[tuple[str, str]]) -> None:
    r = c.get(f"/repos/{repo}/contents/{base}", params={"ref": ref})
    entries = _raise_for(r, f"list {base}")
    for e in entries:
        rel = (prefix + "/" + e["name"]).lstrip("/")
        if e["type"] == "dir":
            _walk_dir(c, repo, f"{base}/{e['name']}", ref, rel, out)
        elif e["type"] == "file":
            blob = _raise_for(
                c.get(f"/repos/{repo}/git/blobs/{e['sha']}"),
                f"blob {rel}",
            )
            content = base64.b64decode(blob["content"]).decode("utf-8", errors="replace")
            out.append((rel, content))


# --- name validation -------------------------------------------------------

def validate_name(name: str) -> str:
    name = name.strip().lower()
    if not NAME_RE.match(name):
        raise GhError(400, "name must match [a-z][a-z0-9-]{0,30}")
    if name in {"gateway", "main", "production", "tools", "templates", "config"}:
        raise GhError(400, f"name {name!r} collides with a gateway-internal path")
    return name


def _validate_port(port: int) -> int:
    if port < 1024 or port > 65535:
        raise GhError(400, "port must be in [1024, 65535]")
    return port


# --- create_service --------------------------------------------------------

@dataclass(frozen=True)
class CreatedProject:
    name: str
    repo_full_name: str         # rndexp-art/svc-<name>
    repo_html_url: str
    seed_commit_sha: str
    feat_branch: str            # e.g. feat/add-<name>
    feat_branch_url: str        # github branch URL


def create_service(name: str, port: int) -> CreatedProject:
    """End-to-end: new repo + scaffolded seed commit + main + production +
    gateway feat/add-<name> branch with the submodule wired up.

    Idempotency: this is NOT idempotent. If a step fails partway, you'll
    have to clean up by hand. Callers should treat it as a one-shot.
    """
    name = validate_name(name)
    port = _validate_port(port)

    with _client() as c:
        # 1. Refuse if the repo or the gateway branch already exist.
        new_repo_full = f"{_org()}/svc-{name}"
        if _exists(c, f"/repos/{new_repo_full}"):
            raise GhError(409, f"repo {new_repo_full} already exists")
        if _branch_exists(c, _gateway_repo(), f"feat/add-{name}"):
            raise GhError(409, f"branch feat/add-{name} already exists on gateway")

        # 2. Create the empty svc repo.
        created_repo = _raise_for(c.post(
            f"/orgs/{_org()}/repos",
            json={
                "name": f"svc-{name}",
                "private": False,
                "description": f"rndexp.art {name} service",
                "auto_init": False,
                "has_issues": True,
                "has_wiki": False,
            },
        ), "create svc repo")

        # 3. Scaffold the seed commit from the gateway templates.
        templates = _fetch_templates(c)
        if not templates:
            raise GhError(502, f"no files found under {GATEWAY_TEMPLATE_PATH} on gateway main")
        seed_commit_sha = _seed_repo_with_template(
            c, repo=new_repo_full, name=name, port=port, templates=templates,
        )

        # 4. Point both branches at the seed commit.
        for branch in ("main", "production"):
            _raise_for(c.post(
                f"/repos/{new_repo_full}/git/refs",
                json={"ref": f"refs/heads/{branch}", "sha": seed_commit_sha},
            ), f"create {branch} on svc")

        # 5. Open feat/add-<name> on the gateway with the submodule wired up.
        feat = _open_gateway_feat_branch(
            c, name=name, svc_seed_commit_sha=seed_commit_sha, svc_repo=new_repo_full,
        )

    return CreatedProject(
        name=name,
        repo_full_name=new_repo_full,
        repo_html_url=created_repo["html_url"],
        seed_commit_sha=seed_commit_sha,
        feat_branch=feat,
        feat_branch_url=f"https://github.com/{_gateway_repo()}/tree/{feat}",
    )


def _exists(c: httpx.Client, path: str) -> bool:
    r = c.get(path)
    if r.status_code == 200:
        return True
    if r.status_code == 404:
        return False
    _raise_for(r, f"probe {path}")
    return False  # unreachable


def _branch_exists(c: httpx.Client, repo: str, branch: str) -> bool:
    r = c.get(f"/repos/{repo}/branches/{branch}")
    return r.status_code == 200


def _seed_repo_with_template(c: httpx.Client, *, repo: str, name: str, port: int,
                              templates: list[tuple[str, str]]) -> str:
    # 1. Create one blob per template file with placeholders substituted.
    tree_entries: list[dict[str, Any]] = []
    for relpath, raw in templates:
        text = raw.replace("<NAME>", name).replace("<PORT>", str(port))
        b = _raise_for(c.post(
            f"/repos/{repo}/git/blobs",
            json={"content": text, "encoding": "utf-8"},
        ), f"blob {relpath}")
        tree_entries.append({
            "path": relpath,
            "mode": "100644",
            "type": "blob",
            "sha": b["sha"],
        })

    # 2. Build a tree from those blobs.
    tree = _raise_for(c.post(
        f"/repos/{repo}/git/trees",
        json={"tree": tree_entries},
    ), "create tree")

    # 3. Single commit with no parents.
    commit = _raise_for(c.post(
        f"/repos/{repo}/git/commits",
        json={
            "message": f"feat: scaffold {name} service",
            "tree": tree["sha"],
            "parents": [],
        },
    ), "create commit")
    return commit["sha"]


# --- gateway feat branch ---------------------------------------------------

def _open_gateway_feat_branch(c: httpx.Client, *, name: str,
                               svc_seed_commit_sha: str, svc_repo: str) -> str:
    """Create feat/add-<name> on the gateway, off production, with:
       - .gitmodules updated to register services/<name> + svc URL
       - config/services.yml updated to enable in both envs
       - a tree entry at services/<name> of mode 160000 (gitlink) pointing at
         svc_seed_commit_sha
    """
    gateway = _gateway_repo()

    # 1. Production base SHA.
    base = _raise_for(c.get(f"/repos/{gateway}/git/ref/heads/production"),
                      "get production ref")
    base_sha = base["object"]["sha"]
    base_commit = _raise_for(c.get(f"/repos/{gateway}/git/commits/{base_sha}"),
                              "get production commit")
    base_tree_sha = base_commit["tree"]["sha"]

    # 2. Read current .gitmodules and config/services.yml from production.
    gitmodules = _read_text_file(c, gateway, "production", ".gitmodules") or ""
    services_yml = _read_text_file(c, gateway, "production", "config/services.yml") or ""

    new_gitmodules = _append_submodule_to_gitmodules(gitmodules, name, svc_repo)
    new_services_yml = _append_service_to_services_yml(services_yml, name)

    # 3. Build new blobs for the modified text files.
    gitmodules_blob = _raise_for(c.post(
        f"/repos/{gateway}/git/blobs",
        json={"content": new_gitmodules, "encoding": "utf-8"},
    ), "create .gitmodules blob")["sha"]
    services_blob = _raise_for(c.post(
        f"/repos/{gateway}/git/blobs",
        json={"content": new_services_yml, "encoding": "utf-8"},
    ), "create services.yml blob")["sha"]

    # 4. New tree, based on the production tree, with three changes:
    #    - replace .gitmodules
    #    - replace config/services.yml
    #    - add services/<name> as a gitlink (submodule pointer)
    new_tree = _raise_for(c.post(
        f"/repos/{gateway}/git/trees",
        json={
            "base_tree": base_tree_sha,
            "tree": [
                {"path": ".gitmodules", "mode": "100644", "type": "blob", "sha": gitmodules_blob},
                {"path": "config/services.yml", "mode": "100644", "type": "blob", "sha": services_blob},
                {"path": f"services/{name}", "mode": "160000", "type": "commit", "sha": svc_seed_commit_sha},
            ],
        },
    ), "create gateway tree")

    # 5. New commit with production as parent.
    commit = _raise_for(c.post(
        f"/repos/{gateway}/git/commits",
        json={
            "message": f"feat({name}): add svc-{name} submodule + enable in both envs",
            "tree": new_tree["sha"],
            "parents": [base_sha],
        },
    ), "create gateway commit")

    branch = f"feat/add-{name}"
    _raise_for(c.post(
        f"/repos/{gateway}/git/refs",
        json={"ref": f"refs/heads/{branch}", "sha": commit["sha"]},
    ), f"create {branch}")
    return branch


def _read_text_file(c: httpx.Client, repo: str, ref: str, path: str) -> str | None:
    r = c.get(f"/repos/{repo}/contents/{path}", params={"ref": ref})
    if r.status_code == 404:
        return None
    data = _raise_for(r, f"read {path}")
    if "content" not in data:
        return None
    return base64.b64decode(data["content"]).decode("utf-8")


def _append_submodule_to_gitmodules(current: str, name: str, repo_full_name: str) -> str:
    block = (
        f'[submodule "services/{name}"]\n'
        f"\tpath = services/{name}\n"
        f"\turl = https://github.com/{repo_full_name}.git\n"
    )
    text = current.rstrip("\n")
    if text:
        text += "\n"
    return text + block


def _append_service_to_services_yml(current: str, name: str) -> str:
    """Add `name` under both `local:` and `production:` keys.

    services.yml is small and human-edited, so we do a textual edit rather
    than a full YAML round-trip — that preserves comments and ordering.
    Falls through to a clean rewrite if the file is empty.
    """
    if not current.strip():
        return (
            "# Per-environment service enablement matrix.\n"
            "# See tools/rndexp service --help.\n\n"
            f"local:\n  - {name}\n\n"
            f"production:\n  - {name}\n"
        )

    out_lines: list[str] = []
    seen_envs: set[str] = set()
    lines = current.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        out_lines.append(line)
        m = re.match(r"^(local|production):\s*(\[\s*\])?\s*$", line)
        if m:
            env = m.group(1)
            seen_envs.add(env)
            empty_inline = m.group(2) is not None
            if empty_inline:
                # Convert "local: []" to "local:\n  - <name>"
                out_lines[-1] = f"{env}:"
                out_lines.append(f"  - {name}")
                i += 1
                continue
            # Walk forward to the end of this list, keeping existing entries,
            # then append `- <name>` if not already present.
            j = i + 1
            existing: list[str] = []
            while j < len(lines) and (lines[j].startswith("  - ") or lines[j].strip() == ""):
                if lines[j].startswith("  - "):
                    existing.append(lines[j][4:].strip())
                out_lines.append(lines[j])
                j += 1
            if name not in existing:
                # Insert the new entry just before the trailing blank line, or
                # at the end of the list if there isn't one.
                while out_lines and out_lines[-1].strip() == "":
                    blank = out_lines.pop()
                    out_lines.append(f"  - {name}")
                    out_lines.append(blank)
                    break
                else:
                    out_lines.append(f"  - {name}")
            i = j
            continue
        i += 1

    for env in ("local", "production"):
        if env not in seen_envs:
            out_lines.append("")
            out_lines.append(f"{env}:")
            out_lines.append(f"  - {name}")

    text = "\n".join(out_lines)
    if not text.endswith("\n"):
        text += "\n"
    return text


# --- listing pending branches ----------------------------------------------

@dataclass(frozen=True)
class PendingBranch:
    name: str                  # the service name, e.g. "wiki"
    branch: str                # the gateway branch, e.g. "feat/add-wiki"
    head_sha: str
    short_sha: str
    last_commit_message: str
    last_commit_at: datetime | None
    branch_html_url: str


def list_pending_branches() -> list[PendingBranch]:
    if not github_client.gh_configured():
        return []
    gateway = _gateway_repo()
    out: list[PendingBranch] = []
    with _client() as c:
        r = c.get(f"/repos/{gateway}/branches", params={"per_page": 100})
        if r.status_code != 200:
            return []
        for b in r.json():
            br = b.get("name", "")
            if not br.startswith("feat/add-"):
                continue
            sha = b.get("commit", {}).get("sha", "")
            if not sha:
                continue
            commit = c.get(f"/repos/{gateway}/git/commits/{sha}")
            msg = ""
            when: datetime | None = None
            if commit.status_code == 200:
                cd = commit.json()
                msg = (cd.get("message", "") or "").splitlines()[0] if cd.get("message") else ""
                when_s = ((cd.get("committer") or {}).get("date")
                          or (cd.get("author") or {}).get("date"))
                when = github_client._parse_dt(when_s)
            out.append(PendingBranch(
                name=br[len("feat/add-"):],
                branch=br,
                head_sha=sha,
                short_sha=sha[:7],
                last_commit_message=msg,
                last_commit_at=when,
                branch_html_url=f"https://github.com/{gateway}/tree/{br}",
            ))
    out.sort(key=lambda p: p.last_commit_at or datetime.min, reverse=True)
    return out


# --- merge to production ---------------------------------------------------

@dataclass(frozen=True)
class MergeResult:
    feat_to_prod_pr_url: str
    prod_to_main_pr_url: str
    notes: list[str]


def merge_to_production(name: str) -> MergeResult:
    """1. Open + merge PR feat/add-<name> → production
       2. Open + merge PR production → main

    If main is already at the same SHA as production after step 1, step 2
    is a no-op and we return a synthetic note instead of failing.
    """
    name = validate_name(name)
    gateway = _gateway_repo()
    feat = f"feat/add-{name}"
    notes: list[str] = []
    with _client() as c:
        if not _branch_exists(c, gateway, feat):
            raise GhError(404, f"branch {feat} not found")

        # 1. Open PR feat → production. Re-use an open one if it exists.
        feat_pr = _ensure_pr(c, gateway, head=feat, base="production",
                              title=f"feat: add {name} service",
                              body=f"Auto-opened by the dashboard for the new {name} submodule.")
        _merge_pr(c, gateway, feat_pr["number"], commit_title=f"feat: add {name} service")
        notes.append(f"merged {feat} → production via PR #{feat_pr['number']}")

        # 2. Open PR production → main, unless they're already even.
        if _branches_equal(c, gateway, "production", "main"):
            notes.append("production and main are even — no main PR needed.")
            return MergeResult(
                feat_to_prod_pr_url=feat_pr["html_url"],
                prod_to_main_pr_url="",
                notes=notes,
            )
        try:
            main_pr = _ensure_pr(c, gateway, head="production", base="main",
                                  title=f"chore: sync production into main ({name})",
                                  body=f"Auto-opened after merging {feat} so main carries the new submodule.")
        except GhError as e:
            # Most common cause: GitHub returns 422 "No commits between
            # production and main" if a race left them already-merged.
            if e.status_code == 422:
                notes.append("production already at main — skipping sync PR.")
                return MergeResult(
                    feat_to_prod_pr_url=feat_pr["html_url"],
                    prod_to_main_pr_url="",
                    notes=notes,
                )
            raise
        _merge_pr(c, gateway, main_pr["number"],
                  commit_title=f"chore: sync production into main ({name})")
        notes.append(f"merged production → main via PR #{main_pr['number']}")

        return MergeResult(
            feat_to_prod_pr_url=feat_pr["html_url"],
            prod_to_main_pr_url=main_pr["html_url"],
            notes=notes,
        )


def _ensure_pr(c: httpx.Client, repo: str, *, head: str, base: str,
               title: str, body: str) -> dict[str, Any]:
    # See if an open PR already exists for this head→base.
    existing = c.get(f"/repos/{repo}/pulls",
                     params={"head": f"{_org()}:{head}", "base": base, "state": "open"})
    if existing.status_code == 200:
        rows = existing.json()
        if rows:
            return rows[0]
    return _raise_for(c.post(
        f"/repos/{repo}/pulls",
        json={"title": title, "body": body, "head": head, "base": base, "draft": False},
    ), f"open PR {head}→{base}")


def _merge_pr(c: httpx.Client, repo: str, number: int, *, commit_title: str) -> None:
    _raise_for(c.put(
        f"/repos/{repo}/pulls/{number}/merge",
        json={"merge_method": "merge", "commit_title": commit_title},
    ), f"merge PR #{number}")


def _branches_equal(c: httpx.Client, repo: str, a: str, b: str) -> bool:
    ra = c.get(f"/repos/{repo}/git/ref/heads/{a}")
    rb = c.get(f"/repos/{repo}/git/ref/heads/{b}")
    if ra.status_code != 200 or rb.status_code != 200:
        return False
    return ra.json()["object"]["sha"] == rb.json()["object"]["sha"]
