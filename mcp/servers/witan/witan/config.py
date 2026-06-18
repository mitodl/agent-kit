import os
from dataclasses import dataclass
from pathlib import Path


# Path to the bundled query files, resolved relative to this file.
_QUERIES_DIR = Path(__file__).parent.parent / "queries"
_DEFAULT_GRAPH_URI = Path.home() / ".local" / "share" / "witan" / "graph.omni"


@dataclass(frozen=True)
class Config:
    graph_uri: str
    """Local path, s3://, or http:// URI pointing at the graph."""

    graph_token: str | None
    """Bearer token. Required when graph_uri is http://. Unused for local/S3."""

    author: str
    """Attribution string written to Memory.author on every insert."""

    queries_dir: Path
    """Directory containing read.gq and mutations.gq."""


def load() -> Config:
    """Load config from environment. All variables are optional with sensible defaults."""
    return Config(
        graph_uri=os.environ.get("WITAN_MEMORY_URI", str(_DEFAULT_GRAPH_URI)),
        graph_token=os.environ.get("WITAN_MEMORY_TOKEN"),
        author=os.environ.get(
            "WITAN_AUTHOR",
            os.environ.get("USER", "unknown"),
        ),
        queries_dir=_QUERIES_DIR,
    )
