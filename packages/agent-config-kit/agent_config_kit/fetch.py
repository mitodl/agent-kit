"""Fetch remote (``https://``/``http://``/``git+``) sources referenced by a
manifest's ``skill_md_path``/``entry_path`` fields and cache them locally, so
``installers.py``/``plan.py``/``prune.py`` never need to know a path didn't
originate on the local filesystem — they only ever see a resolved, absolute
``Path`` (spec M5's existing contract, unchanged).

Stdlib-only (``urllib.request`` for HTTP(S), the ``git`` binary via
``subprocess`` for ``git+`` URIs) — no new dependency on the base package
(spec D3): ``load_manifest`` already touches the filesystem to resolve
relative paths, so fetching a remote source at load time is an extension of
that same step, not a reason to gate remote manifests behind an extra.

A plain ``https://``/``http://`` URI fetches exactly one file — sufficient
for ``entry_path`` (a single plugin script) and a single-file skill, but not
a skill with supporting files (``scripts/``, ``references/``, ...), since
there is no directory to walk on the other end of one HTTP GET. A skill that
needs those must use a ``git+`` URI with ``#subdirectory=`` instead, which
clones the whole tree.

Caching/staleness: every ``load_manifest()`` call re-fetches. HTTP(S) uses a
conditional GET (``If-None-Match``/``If-Modified-Since`` against a stored
ETag/Last-Modified sidecar) so an unchanged remote is a cheap 304, and a
transient network failure (no response at all) falls back to whatever was
cached from the last successful fetch rather than hard-failing an
``apply``/``validate`` that would otherwise have worked offline. A real HTTP
error response (404, 403, ...) is NOT treated as transient and always
raises, even if a stale cache exists — that response means the resource is
actually gone, not just unreachable this instant. ``git+`` URIs re-fetch via
a shallow ``git fetch``, falling back the same way on a ``git`` failure if a
prior checkout exists.

Prune implication (see ``prune.py``): a fetched file is tracked by
``skill_files``/``hook_identity`` exactly like any other local file, so no
``prune.py`` changes were needed. But this also means content drift for an
*unchanged* URI is invisible between two applies unless a fetch actually
happens to notice the change — matching the same "content-level drift is
never actionable, only presence/absence is" reasoning the CLI spec's
``validate`` design already established for ordinary local skills (cli spec
§4.2).
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit

_HTTP_SCHEMES = ("http://", "https://")
_GIT_PREFIX = "git+"
_SUBDIRECTORY_RE = re.compile(r"#subdirectory=(.+)$")


class FetchError(Exception):
    """Raised when a remote URI can't be fetched and no usable cache exists."""


def is_remote_uri(value: str) -> bool:
    return value.startswith(_HTTP_SCHEMES) or value.startswith(_GIT_PREFIX)


def _cache_key(uri: str) -> str:
    return hashlib.sha256(uri.encode("utf-8")).hexdigest()[:16]


def _meta_path(dest: Path) -> Path:
    return dest.with_name(dest.name + ".meta.json")


def _fetch_http_file(uri: str, dest: Path) -> Path:
    meta_path = _meta_path(dest)
    headers: dict[str, str] = {}
    if dest.is_file() and meta_path.is_file():
        try:
            prior_meta = json.loads(meta_path.read_text())
        except (json.JSONDecodeError, OSError):
            prior_meta = {}  # corrupted/partial sidecar — fetch unconditionally
        if etag := prior_meta.get("etag"):
            headers["If-None-Match"] = etag
        if last_modified := prior_meta.get("last_modified"):
            headers["If-Modified-Since"] = last_modified

    request = urllib.request.Request(uri, headers=headers)  # noqa: S310
    try:
        with urllib.request.urlopen(request, timeout=15) as response:  # noqa: S310
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(response.read())
            meta_path.write_text(
                json.dumps(
                    {
                        "etag": response.headers.get("ETag"),
                        "last_modified": response.headers.get("Last-Modified"),
                    }
                )
            )
    except urllib.error.HTTPError as exc:
        if exc.code == 304 and dest.is_file():
            return dest  # cached copy confirmed still fresh
        raise FetchError(f"could not fetch {uri}: HTTP {exc.code}") from exc
    except urllib.error.URLError as exc:
        if dest.is_file():
            return dest  # transient network failure — fall back to cache
        raise FetchError(f"could not fetch {uri}: {exc.reason}") from exc
    return dest


def _run_git(args: list[str], *, cwd: Path | None = None) -> None:
    result = subprocess.run(  # noqa: S603
        ["git", *args], cwd=cwd, capture_output=True, text=True, timeout=60
    )
    if result.returncode != 0:
        raise FetchError(f"git {' '.join(args)} failed: {result.stderr.strip()}")


def _parse_git_uri(uri: str) -> tuple[str, str | None, str | None]:
    """``git+<url>[@ref][#subdirectory=<path>]`` -> ``(clone_url, ref, subdirectory)``.

    ``ref`` is only recognized after the last ``/`` or ``:`` in the URL, i.e.
    after the repo path — matching the common ``...repo.git@v1.0.0``
    convention — so it can't be confused with a userinfo ``@`` earlier in
    the URL (e.g. ``https://user@host/...``, which has no ref). The ``:``
    half of that also covers SCP-like syntax (``git@host:org/repo.git`` or,
    with no ``/`` at all, ``git@host:repo.git@v1.0.0``), where the repo path
    is separated from the host by ``:`` rather than a URL scheme's ``/``."""
    rest = uri[len(_GIT_PREFIX) :]
    subdirectory = None
    if match := _SUBDIRECTORY_RE.search(rest):
        subdirectory = match.group(1)
        rest = rest[: match.start()]
    ref = None
    last_sep = max(rest.rfind("/"), rest.rfind(":"))
    at_idx = rest.find("@", last_sep) if last_sep != -1 else rest.rfind("@")
    if at_idx != -1:
        ref = rest[at_idx + 1 :]
        rest = rest[:at_idx]
    return rest, ref, subdirectory


def _fetch_git(uri: str, dest: Path) -> Path:
    clone_url, ref, subdirectory = _parse_git_uri(uri)
    if shutil.which("git") is None:
        raise FetchError(
            f"could not fetch {uri}: `git` is required for git+ URIs but was "
            "not found on PATH"
        )

    is_fresh_checkout = not (dest / ".git").is_dir()
    if is_fresh_checkout:
        shutil.rmtree(dest, ignore_errors=True)
        dest.mkdir(parents=True, exist_ok=True)
        _run_git(["init"], cwd=dest)
        _run_git(["remote", "add", "origin", clone_url], cwd=dest)

    try:
        _run_git(["fetch", "--depth", "1", "origin", ref or "HEAD"], cwd=dest)
        _run_git(["checkout", "--detach", "FETCH_HEAD"], cwd=dest)
    except FetchError:
        if not is_fresh_checkout and any(dest.iterdir()):
            pass  # transient failure — reuse whatever's already checked out
        else:
            shutil.rmtree(dest, ignore_errors=True)
            raise

    return dest / subdirectory if subdirectory else dest


def fetch_remote(uri: str, cache_dir: Path) -> Path:
    """Fetch/refresh ``uri`` into ``cache_dir``, returning the local path to
    substitute for the manifest's original value. Idempotent and safe to call
    on every ``load_manifest()`` — see the module docstring for the
    caching/staleness policy."""
    key = _cache_key(uri)
    if uri.startswith(_GIT_PREFIX):
        return _fetch_git(uri, cache_dir / key)
    basename = Path(urlsplit(uri).path).name or "download"
    return _fetch_http_file(uri, cache_dir / key / basename)
