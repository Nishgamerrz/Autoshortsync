"""Git and GitHub-release helpers used to store the backup."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

API_BASE = "https://api.github.com"
UPLOADS_BASE = "https://uploads.github.com"

#: GitHub release assets can be 2 GB, but reading a huge file into memory is a
#: bad idea. Shorts are small; anything above this falls back to ``gh`` if it
#: is installed, otherwise the upload is skipped with a clear message.
MAX_DIRECT_UPLOAD_BYTES = 200 * 1024 * 1024


class GitHubError(RuntimeError):
    pass


def run_git(args: list[str], cwd, check: bool = True) -> str:
    proc = subprocess.run(
        ["git", *args], cwd=str(cwd), text=True, capture_output=True
    )
    if check and proc.returncode != 0:
        raise GitHubError(
            f"git {' '.join(args)} failed (exit {proc.returncode}): "
            f"{(proc.stderr or proc.stdout).strip()}"
        )
    return (proc.stdout or "").strip()


def parse_repo(url: str) -> str:
    """Normalize ``git@github.com:owner/repo.git`` / HTTPS URLs to ``owner/repo``."""
    url = (url or "").strip().rstrip("/")
    if url.endswith(".git"):
        url = url[:-4]
    for marker in ("github.com/", "github.com:"):
        if marker in url:
            return url.split(marker, 1)[1]
    return ""


def current_repo_slug() -> str:
    try:
        url = run_git(["config", "--get", "remote.origin.url"], cwd=Path.cwd())
        if url:
            return parse_repo(url)
    except GitHubError:
        pass
    return os.environ.get("GITHUB_REPOSITORY", "")


def current_branch(repo_dir) -> str:
    branch = run_git(["rev-parse", "--abbrev-ref", "HEAD"], cwd=repo_dir, check=False)
    return branch if branch and branch != "HEAD" else ""


def is_git_repo(repo_dir) -> bool:
    proc = subprocess.run(
        ["git", "rev-parse", "--git-dir"],
        cwd=str(repo_dir),
        text=True,
        capture_output=True,
    )
    return proc.returncode == 0


def ensure_branch(repo_dir, branch: str) -> None:
    if not is_git_repo(repo_dir):
        return
    try:
        run_git(["rev-parse", "--verify", branch], cwd=repo_dir)
        run_git(["checkout", branch], cwd=repo_dir)
    except GitHubError:
        run_git(["checkout", "--orphan", branch], cwd=repo_dir)


def configure_git(repo_dir, name: str, email: str) -> None:
    run_git(["config", "user.name", name], cwd=repo_dir)
    run_git(["config", "user.email", email], cwd=repo_dir)


def clone_repo(repo: str, token: str, dest) -> Path:
    dest = Path(dest)
    if dest.exists():
        shutil.rmtree(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    url = (
        f"https://x-access-token:{token}@github.com/{repo}.git"
        if token
        else f"https://github.com/{repo}.git"
    )
    run_git(["clone", "--depth", "1", url, str(dest)], cwd=dest.parent)
    return dest


def has_changes(repo_dir) -> bool:
    return bool(run_git(["status", "--porcelain"], cwd=repo_dir))


def commit_all(repo_dir, message: str) -> bool:
    run_git(["add", "-A"], cwd=repo_dir)
    if not has_changes(repo_dir):
        return False
    run_git(["commit", "-m", message], cwd=repo_dir)
    return True


def build_remote_url(repo: str, token: str) -> str | None:
    if not repo or not token:
        return None
    return f"https://x-access-token:{token}@github.com/{repo}.git"


def push(repo_dir, remote_url: str | None, branch: str) -> None:
    if remote_url:
        run_git(["push", remote_url, f"HEAD:{branch}"], cwd=repo_dir)
    else:
        run_git(["push", "origin", f"HEAD:{branch}"], cwd=repo_dir)


def _api(method: str, url: str, token: str, payload: dict | None = None) -> dict:
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    data = None
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"

    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=90) as response:
            body = response.read().decode("utf-8")
            return json.loads(body) if body else {}
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")
        raise GitHubError(
            f"GitHub API {method} {url} -> HTTP {exc.code}: {body[:500]}"
        ) from exc


def repo_info(repo: str, token: str) -> dict:
    return _api("GET", f"{API_BASE}/repos/{repo}", token)


def get_release(repo: str, token: str, tag: str) -> dict | None:
    try:
        return _api("GET", f"{API_BASE}/repos/{repo}/releases/tags/{tag}", token)
    except GitHubError as exc:
        if "HTTP 404" in str(exc):
            return None
        raise


def create_release(repo: str, token: str, tag: str, name: str, body: str) -> dict:
    return _api(
        "POST",
        f"{API_BASE}/repos/{repo}/releases",
        token,
        {
            "tag_name": tag,
            "name": name,
            "body": body,
            "draft": False,
            "prerelease": False,
        },
    )


def get_or_create_release(
    repo: str, token: str, tag: str, name: str, body: str
) -> dict:
    release = get_release(repo, token, tag)
    if release:
        return release
    try:
        return create_release(repo, token, tag, name, body)
    except GitHubError as exc:
        if "already_exists" in str(exc) or "HTTP 422" in str(exc):
            release = get_release(repo, token, tag)
            if release:
                return release
        raise


def list_release_assets(repo: str, token: str, release_id: int) -> list[dict]:
    return _api(
        "GET",
        f"{API_BASE}/repos/{repo}/releases/{release_id}/assets?per_page=100",
        token,
    )


def upload_release_asset(
    repo: str, token: str, release_id: int, file_path, name: str
) -> dict:
    file_path = Path(file_path)
    size = file_path.stat().st_size
    if size > MAX_DIRECT_UPLOAD_BYTES:
        raise GitHubError(
            f"{file_path.name} is {size / 1e6:.0f} MB; too large for the direct "
            f"upload path (limit {MAX_DIRECT_UPLOAD_BYTES // (1024 * 1024)} MB). "
            "Install the GitHub CLI or reduce the video size."
        )

    data = file_path.read_bytes()
    url = (
        f"{UPLOADS_BASE}/repos/{repo}/releases/{release_id}/assets"
        f"?name={urllib.parse.quote(name)}"
    )
    request = urllib.request.Request(
        url,
        data=data,
        method="POST",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/octet-stream",
            "Content-Length": str(len(data)),
            "Accept": "application/vnd.github+json",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=600) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")
        raise GitHubError(
            f"Release asset upload failed -> HTTP {exc.code}: {body[:400]}"
        ) from exc
