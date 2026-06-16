import json
import os
import shutil
import subprocess
from pathlib import Path


class OmnigraphClient:
    """Subprocess wrapper for the omnigraph CLI."""

    def __init__(
        self,
        graph_uri: str,
        queries_dir: Path,
        token: str | None = None,
    ) -> None:
        self.graph_uri = graph_uri
        self.queries_dir = queries_dir
        self.token = token
        self._binary = self._find_binary()

    # ── Public API ────────────────────────────────────────────────

    def read(
        self,
        query_file: str,
        query_name: str,
        params: dict,
    ) -> list[dict]:
        """Run a named read query. Returns a list of result rows."""
        result = self._run(
            "query",
            "--query",
            str(self.queries_dir / query_file),
            query_name,
            "--params",
            json.dumps(params),
            "--format",
            "json",
        )
        if not result.strip():
            return []
        try:
            parsed = json.loads(result)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"omnigraph returned non-JSON: {result!r}") from exc
        # v0.7.0 wraps results in {rows: [...], columns: [...], ...}
        rows = parsed.get("rows", parsed) if isinstance(parsed, dict) else parsed
        # strip alias prefixes: "p.slug" → "slug"
        return [{k.split(".", 1)[-1]: v for k, v in row.items()} for row in rows]

    def change(
        self,
        query_file: str,
        query_name: str,
        params: dict,
    ) -> None:
        """Run a named mutation query."""
        self._run(
            "mutate",
            "--query",
            str(self.queries_dir / query_file),
            query_name,
            "--params",
            json.dumps(params),
        )

    # ── Internals ─────────────────────────────────────────────────

    def _run(self, subcommand: str, *args: str) -> str:
        cmd = [self._binary, subcommand, "--store", self.graph_uri, *args]
        env = dict(os.environ)
        if self.token:
            env["OMNIGRAPH_SERVER_BEARER_TOKEN"] = self.token

        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            env=env,
        )
        if result.returncode != 0:
            raise RuntimeError(
                f"omnigraph {subcommand} failed (exit {result.returncode}):\n"
                f"{result.stderr.strip()}"
            )
        return result.stdout

    @staticmethod
    def _find_binary() -> str:
        binary = shutil.which("omnigraph")
        if binary is None:
            raise RuntimeError(
                "omnigraph binary not found on PATH. "
                "Run mcp/servers/omnigraph-memory/install.sh first."
            )
        return binary
