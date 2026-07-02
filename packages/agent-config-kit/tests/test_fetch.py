import http.server
import shutil
import subprocess
import threading
from pathlib import Path

import pytest

from agent_config_kit.fetch import FetchError, fetch_remote, is_remote_uri

requires_git = pytest.mark.skipif(
    shutil.which("git") is None, reason="git binary not on PATH"
)


def test_is_remote_uri_recognizes_http_https_and_git_plus():
    assert is_remote_uri("https://raw.githubusercontent.com/org/repo/main/SKILL.md")
    assert is_remote_uri("http://example.com/x.ts")
    assert is_remote_uri("git+https://github.com/org/repo.git")
    assert is_remote_uri("git+file:///tmp/repo")


def test_is_remote_uri_rejects_local_paths():
    assert not is_remote_uri("skills/witan-task/SKILL.md")
    assert not is_remote_uri("/abs/path/SKILL.md")


class _EtagHandler(http.server.BaseHTTPRequestHandler):
    body = b"# skill\n"
    etag = '"v1"'
    requests = 0

    def do_GET(self):  # noqa: N802
        type(self).requests += 1
        if self.headers.get("If-None-Match") == self.etag:
            self.send_response(304)
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("ETag", self.etag)
        self.send_header("Content-Length", str(len(self.body)))
        self.end_headers()
        self.wfile.write(self.body)

    def log_message(self, *args):  # noqa: D102
        pass


@pytest.fixture
def http_server():
    handler = type("Handler", (_EtagHandler,), {"requests": 0})
    server = http.server.HTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server, handler
    finally:
        server.shutdown()
        thread.join()


def test_fetch_http_file_writes_content_and_meta(tmp_path, http_server):
    server, _handler = http_server
    uri = f"http://127.0.0.1:{server.server_port}/SKILL.md"

    dest = fetch_remote(uri, tmp_path / "cache")

    assert dest.name == "SKILL.md"
    assert dest.read_bytes() == b"# skill\n"


def test_fetch_http_file_reuses_cache_on_304(tmp_path, http_server):
    server, handler = http_server
    uri = f"http://127.0.0.1:{server.server_port}/SKILL.md"
    cache_dir = tmp_path / "cache"

    fetch_remote(uri, cache_dir)
    assert handler.requests == 1
    dest = fetch_remote(uri, cache_dir)
    assert handler.requests == 2  # second call issued a conditional GET

    assert dest.read_bytes() == b"# skill\n"


def test_fetch_http_file_missing_url_raises_fetch_error(tmp_path, http_server):
    server, _handler = http_server
    uri = f"http://127.0.0.1:{server.server_port}/does-not-exist.md"

    class NotFoundHandler(_EtagHandler):
        def do_GET(self):  # noqa: N802
            self.send_response(404)
            self.end_headers()

    server.RequestHandlerClass = NotFoundHandler

    with pytest.raises(FetchError, match="404"):
        fetch_remote(uri, tmp_path / "cache")


def test_fetch_http_file_falls_back_to_cache_on_connection_failure(tmp_path):
    # Nothing is listening on this port — a connection error, not an HTTP
    # error response — so a prior successful fetch should be reused.
    from agent_config_kit.fetch import _fetch_http_file

    dest = tmp_path / "cache" / "SKILL.md"
    dest.parent.mkdir(parents=True)
    dest.write_text("# cached copy\n")

    fallback = _fetch_http_file("http://127.0.0.1:1/SKILL.md", dest)

    assert fallback == dest
    assert fallback.read_text() == "# cached copy\n"


def _init_git_repo(path: Path) -> None:
    path.mkdir(parents=True)
    subprocess.run(["git", "init", "--quiet"], cwd=path, check=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"], cwd=path, check=True
    )
    subprocess.run(["git", "config", "user.name", "Test"], cwd=path, check=True)
    (path / "skills" / "my-skill").mkdir(parents=True)
    (path / "skills" / "my-skill" / "SKILL.md").write_text("# my-skill\n")
    subprocess.run(["git", "add", "."], cwd=path, check=True)
    subprocess.run(["git", "commit", "--quiet", "-m", "init"], cwd=path, check=True)


@requires_git
def test_fetch_git_clones_and_resolves_subdirectory(tmp_path):
    repo = tmp_path / "origin"
    _init_git_repo(repo)
    uri = f"git+file://{repo}#subdirectory=skills/my-skill"

    dest = fetch_remote(uri, tmp_path / "cache")

    assert dest.name == "my-skill"
    assert dest.parent.name == "skills"
    assert (dest / "SKILL.md").read_text() == "# my-skill\n"


@requires_git
def test_fetch_git_is_idempotent_across_calls(tmp_path):
    repo = tmp_path / "origin"
    _init_git_repo(repo)
    uri = f"git+file://{repo}#subdirectory=skills/my-skill"
    cache_dir = tmp_path / "cache"

    first = fetch_remote(uri, cache_dir)
    second = fetch_remote(uri, cache_dir)

    assert first == second
    assert (second / "SKILL.md").read_text() == "# my-skill\n"


def test_fetch_git_missing_git_binary_raises_fetch_error(tmp_path, monkeypatch):
    import agent_config_kit.fetch as fetch_module

    monkeypatch.setattr(fetch_module.shutil, "which", lambda _: None)

    with pytest.raises(FetchError, match="git.*PATH"):
        fetch_remote("git+file:///nonexistent", tmp_path / "cache")


@pytest.mark.parametrize(
    ("uri", "expected_url", "expected_ref"),
    [
        (
            "git+https://github.com/org/repo.git",
            "https://github.com/org/repo.git",
            None,
        ),
        (
            "git+https://github.com/org/repo.git@v1.0.0",
            "https://github.com/org/repo.git",
            "v1.0.0",
        ),
        (
            "git+https://user@github.com/org/repo.git",
            "https://user@github.com/org/repo.git",
            None,
        ),
        # SCP-like syntax with a path segment: the ":" separates host from
        # path, same as a URL scheme's "/" would.
        (
            "git+git@github.com:org/repo.git@v1.0.0",
            "git@github.com:org/repo.git",
            "v1.0.0",
        ),
        # SCP-like syntax with NO "/" anywhere — regression case for the
        # previous rfind("/")-only heuristic, which returned -1 here and
        # broke ref detection entirely.
        (
            "git+git@github.com:repo.git@v1.0.0",
            "git@github.com:repo.git",
            "v1.0.0",
        ),
    ],
)
def test_parse_git_uri_resolves_ref_across_url_shapes(uri, expected_url, expected_ref):
    from agent_config_kit.fetch import _parse_git_uri

    clone_url, ref, _subdirectory = _parse_git_uri(uri)

    assert clone_url == expected_url
    assert ref == expected_ref
