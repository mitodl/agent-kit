# /// script
# requires-python = ">=3.11"
# dependencies = ["cyclopts>=3"]
# ///
"""Link existing local checkouts or clone the repos in scope into <workdir>/repos."""

import json
import os
import re
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import cyclopts
from common import load_subject, repo_dirname

app = cyclopts.App()
SKIP_DIRS = {"node_modules", "venv", "site-packages", "vendor", "dist", "build"}


def _local_checkouts(roots: list[str]) -> dict[str, Path]:
    """Map owner/name -> path for git checkouts under the given roots (depth <= 4).

    Stops descending at the first checkout and skips dependency/venv dirs, so a
    root like ~/code doesn't turn into a walk of every node_modules tree.
    """
    found: dict[str, Path] = {}
    checkouts: list[Path] = []
    for root in roots:
        base = Path(root).expanduser()
        if not base.is_dir():
            continue
        for dirpath, dirnames, _ in os.walk(base):
            current = Path(dirpath)
            if ".git" in dirnames:
                checkouts.append(current)
                dirnames.clear()
            elif len(current.relative_to(base).parts) >= 4:
                dirnames.clear()
            else:
                dirnames[:] = [
                    d for d in dirnames if not d.startswith(".") and d not in SKIP_DIRS
                ]
    for checkout in checkouts:
        url = subprocess.run(
            ["git", "-C", str(checkout), "remote", "get-url", "origin"],
            capture_output=True,
            text=True,
            check=False,
        ).stdout
        match = re.search(r"github\.com[:/](.+?)(?:\.git)?\s*$", url)
        if match:
            found.setdefault(match.group(1).lower(), checkout)
    return found


def _acquire(
    workdir: Path, repo: str, commits: int, subject: dict, local: dict[str, Path]
) -> dict:
    dest = workdir / "repos" / repo_dirname(repo)
    entry = {
        "repo": repo,
        "commits": commits,
        "path": str(dest),
        "local": False,
        "partial": False,
    }
    if repo.lower() in local:
        if not dest.exists():
            dest.symlink_to(local[repo.lower()])
        entry["local"] = True
        return entry
    partial = commits < subject["full_clone_min_commits"]
    entry["partial"] = partial
    if not dest.exists():
        cmd = (
            ["git", "clone", "-q"]
            + (["--filter=blob:none"] if partial else [])
            + [f"https://github.com/{repo}", str(dest)]
        )
        result = subprocess.run(cmd, capture_output=True, text=True, check=False)
        if result.returncode != 0:
            entry["error"] = result.stderr.strip()[:300]
    return entry


@app.default
def acquire(workdir: Path, jobs: int = 8) -> None:
    """Write repos/manifest.json.

    Repos below full_clone_min_commits are cloned blobless (history only), which is
    enough for commit messages. Diff sampling skips them because every `git show`
    on a blobless clone lazily fetches blobs one round-trip at a time.
    Local checkouts are symlinked and must be treated as read-only.
    """
    subject = load_subject(workdir)
    (workdir / "repos").mkdir(exist_ok=True)
    targets: dict[str, int] = {}
    tsv = workdir / "activity" / "repos.tsv"
    if tsv.exists():
        for line in tsv.read_text().splitlines()[1:]:
            commits, repo, *_ = line.split("\t")
            if int(commits) >= subject["min_commits"]:
                targets[repo] = int(commits)
    for repo in subject["repos"]:
        targets.setdefault(repo, subject["full_clone_min_commits"])
    local = _local_checkouts(subject["local_roots"])
    ordered = sorted(targets.items(), key=lambda item: -item[1])
    with ThreadPoolExecutor(max_workers=jobs) as pool:
        manifest = list(
            pool.map(
                lambda item: _acquire(workdir, item[0], item[1], subject, local),
                ordered,
            )
        )
    (workdir / "repos" / "manifest.json").write_text(json.dumps(manifest, indent=2))
    errors = [m for m in manifest if "error" in m]
    print(
        f"{len(manifest)} repos ({sum(m['local'] for m in manifest)} linked local, "
        f"{sum(m['partial'] for m in manifest)} blobless, {len(errors)} failed)"
    )
    for entry in errors:
        print(f"  failed {entry['repo']}: {entry['error']}")


@app.command
def cleanup(workdir: Path) -> None:
    """Remove clones. Unlinks symlinked local checkouts first so their contents are never deleted."""
    repos_dir = workdir / "repos"
    for child in repos_dir.iterdir():
        if child.is_symlink():
            child.unlink()
    subprocess.run(["rm", "-rf", str(repos_dir)], check=True)
    print(f"removed {repos_dir}")


if __name__ == "__main__":
    app()
