"""Per-agent installation helpers for ``witan setup``.

Each ``install_<agent>()`` function handles the MCP config, skills, and
hook/extension installation for one coding-agent platform. All functions
are best-effort when ``dry_run=True`` — they print what would happen
without writing anything.
"""

from __future__ import annotations

import json
import platform
import re
import shutil
from pathlib import Path

from rich.console import Console

console = Console()

# ── MCP server entry builders ─────────────────────────────────────────────────

_WITAN_ARGS = [
    "--from",
    "git+https://github.com/mitodl/agent-kit#subdirectory=mcp/servers/witan",
    "--with",
    "git+https://github.com/mitodl/agent-kit#subdirectory=mcp/servers/witan-code",
    "witan",
    "serve",
]


def _mcp_entry(author: str, **extra: object) -> dict:
    return {
        **extra,
        "command": "uvx",
        "args": _WITAN_ARGS,
        "env": {"WITAN_AUTHOR": author},
    }


# ── Shared file-level helpers ─────────────────────────────────────────────────


def _load_json(path: Path) -> dict | None:
    """Return parsed JSON from path, or None if the file exists but can't be parsed.

    Handles JSONC (VS Code settings.json allows // comments and trailing commas)
    via a best-effort stripping pass before standard JSON parse.
    Returns an empty dict for a missing file; None signals a parse failure so
    callers can skip writing rather than silently overwriting the user's config.
    """
    if not path.exists():
        return {}
    text = path.read_text()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    # Best-effort JSONC → JSON stripping for VS Code settings files.
    stripped = re.sub(r"/\*.*?\*/", "", text, flags=re.DOTALL)
    stripped = re.sub(r"//[^\n]*", "", stripped)
    stripped = re.sub(r",(\s*[}\]])", r"\1", stripped)
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        return None


def _write_json(path: Path, data: dict, dry_run: bool) -> None:
    if not dry_run:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2) + "\n")


def _install_skills(pkg_dir: Path, dest_base: Path, dry_run: bool) -> None:
    skills_src = pkg_dir / "skills"
    if not skills_src.is_dir():
        return
    for skill_dir in sorted(skills_src.iterdir()):
        skill_md = skill_dir / "SKILL.md"
        if not skill_md.exists():
            continue
        dest = dest_base / skill_dir.name / "SKILL.md"
        if not dry_run:
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(skill_md, dest)
        console.print(f"  [green]skill[/green] /{skill_dir.name} → {dest}")


def _install_files(
    src_dir: Path,
    dest_dir: Path,
    *,
    suffix: str,
    label: str,
    dry_run: bool,
    executable: bool = False,
) -> None:
    if not src_dir.is_dir():
        return
    for src_file in sorted(src_dir.iterdir()):
        if src_file.suffix != suffix:
            continue
        dest = dest_dir / src_file.name
        if not dry_run:
            dest_dir.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src_file, dest)
            if executable:
                dest.chmod(0o755)
        console.print(f"  [green]{label}[/green] {src_file.name} → {dest}")


def _vscode_user_dir() -> Path:
    if platform.system() == "Darwin":
        return Path.home() / "Library" / "Application Support" / "Code" / "User"
    return Path.home() / ".config" / "Code" / "User"


# ── Omnigraph binary ──────────────────────────────────────────────────────────

_OMNIGRAPH_VERSION = "0.7.0"
_OMNIGRAPH_ASSETS: dict[tuple[str, str], str] = {
    ("linux", "x86_64"): "omnigraph-linux-x86_64.tar.gz",
    ("darwin", "arm64"): "omnigraph-macos-arm64.tar.gz",
}


def _download_omnigraph(dest: Path, dry_run: bool) -> None:
    import tarfile
    import tempfile
    import urllib.request

    key = (platform.system().lower(), platform.machine().lower())
    asset = _OMNIGRAPH_ASSETS.get(key)
    if asset is None:
        console.print(
            f"  [yellow]omnigraph[/yellow] — no pre-built binary for"
            f" {key[0]}/{key[1]}; install manually"
        )
        return

    url = (
        f"https://github.com/ModernRelay/omnigraph/releases/download"
        f"/v{_OMNIGRAPH_VERSION}/{asset}"
    )
    console.print(f"  downloading omnigraph v{_OMNIGRAPH_VERSION} …")

    if dry_run:
        console.print(f"  [green]omnigraph[/green] → {dest} [dim](dry-run)[/dim]")
        return

    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory() as tmp:
            archive = Path(tmp) / asset
            with (
                urllib.request.urlopen(url, timeout=60) as resp,
                open(archive, "wb") as fh,
            ):
                fh.write(resp.read())
            with tarfile.open(archive) as tf:
                for member in tf.getmembers():
                    if member.name.split("/")[-1] == "omnigraph" and not member.isdir():
                        f = tf.extractfile(member)
                        if f:
                            dest.write_bytes(f.read())
                        break
        if dest.exists():
            dest.chmod(0o755)
            console.print(f"  [green]omnigraph[/green] → {dest}")
        else:
            console.print(
                "  [red]omnigraph[/red] — binary not found in archive; install manually"
            )
    except Exception as exc:  # noqa: BLE001
        console.print(
            f"  [red]omnigraph download failed[/red] ({exc}); install manually"
        )


def install_omnigraph(pkg_dir: Path, dry_run: bool) -> None:
    local_bin = Path.home() / ".local" / "bin"
    dest = local_bin / "omnigraph"

    bundled = pkg_dir / "_bin" / "omnigraph"
    if bundled.exists():
        if not dry_run:
            local_bin.mkdir(parents=True, exist_ok=True)
            shutil.copy2(bundled, dest)
            dest.chmod(0o755)
        console.print(f"  [green]omnigraph[/green] (bundled) → {dest}")
    else:
        # Not bundled — git/dev install or unsupported platform at build time.
        # Download directly from GitHub releases.
        _download_omnigraph(dest, dry_run)


# ── Claude Code ───────────────────────────────────────────────────────────────


def install_claude(pkg_dir: Path, author: str, dry_run: bool) -> None:
    _install_skills(pkg_dir, Path.home() / ".claude" / "skills", dry_run)
    _install_files(
        pkg_dir / "hooks",
        Path.home() / ".claude" / "hooks",
        suffix=".sh",
        label="hook",
        dry_run=dry_run,
        executable=True,
    )
    settings_path = Path.home() / ".claude" / "settings.json"
    settings = _load_json(settings_path)
    if settings is None:
        console.print(
            f"  [yellow]skip settings.json[/yellow] — could not parse {settings_path}; add witan manually"
        )
        return
    settings.setdefault("mcpServers", {})["witan"] = _mcp_entry(author)
    _merge_claude_hooks(settings)
    _write_json(settings_path, settings, dry_run)
    console.print(f"  [green]settings.json[/green] → {settings_path}")


def _merge_claude_hooks(settings: dict) -> None:
    for event, cmd in (
        ("UserPromptSubmit", "witan inject-context"),
        ("Stop", "witan session-checkpoint"),
    ):
        entry = {"matcher": "", "hooks": [{"type": "command", "command": cmd}]}
        existing = settings.setdefault("hooks", {}).setdefault(event, [])
        if not any(
            any(h.get("command") == cmd for h in e.get("hooks", [])) for e in existing
        ):
            existing.append(entry)


# ── Pi ────────────────────────────────────────────────────────────────────────


def install_pi(pkg_dir: Path, author: str, dry_run: bool) -> None:
    # Skills in both the shared pool (~/.agents/skills/) and Pi-specific dir.
    _install_skills(pkg_dir, Path.home() / ".agents" / "skills", dry_run)
    _install_skills(pkg_dir, Path.home() / ".pi" / "agent" / "skills", dry_run)
    # Pi TypeScript extensions (thin wrappers calling `witan` / `witan-code` CLIs).
    _install_files(
        pkg_dir / "extensions" / "pi",
        Path.home() / ".pi" / "agent" / "extensions",
        suffix=".ts",
        label="extension",
        dry_run=dry_run,
    )
    # MCP config — Pi uses the same mcpServers shape as Claude Code.
    pi_mcp = Path.home() / ".pi" / "agent" / "mcp.json"
    cfg = _load_json(pi_mcp)
    if cfg is None:
        console.print(f"  [yellow]skip mcp.json[/yellow] — could not parse {pi_mcp}")
        return
    cfg.setdefault("mcpServers", {})["witan"] = _mcp_entry(author)
    _write_json(pi_mcp, cfg, dry_run)
    console.print(f"  [green]mcp.json[/green] → {pi_mcp}")


# ── GitHub Copilot ────────────────────────────────────────────────────────────


def install_copilot(pkg_dir: Path, author: str, dry_run: bool) -> None:
    # VS Code 1.99+ supports a global user-level MCP config at
    # <vscode-user-dir>/mcp.json.  The "servers" key and "type":"stdio" field
    # are required by the Copilot MCP adapter.
    mcp_path = _vscode_user_dir() / "mcp.json"
    cfg = _load_json(mcp_path)
    if cfg is None:
        console.print(f"  [yellow]skip mcp.json[/yellow] — could not parse {mcp_path}")
        return
    cfg.setdefault("servers", {})["witan"] = _mcp_entry(author, type="stdio")
    _write_json(mcp_path, cfg, dry_run)
    console.print(f"  [green]mcp.json[/green] → {mcp_path}")
    console.print(
        '  [dim]Ensure[/dim] "github.copilot.chat.mcp.enabled": true '
        "[dim]is set in VS Code settings.[/dim]"
    )


# ── OpenCode ──────────────────────────────────────────────────────────────────


def install_opencode(pkg_dir: Path, author: str, dry_run: bool) -> None:
    # OpenCode (SST) stores its config at ~/.config/opencode/config.json.
    # MCP servers live under the top-level "mcp" key with no "type" field.
    cfg_path = Path.home() / ".config" / "opencode" / "config.json"
    cfg = _load_json(cfg_path)
    if cfg is None:
        console.print(
            f"  [yellow]skip config.json[/yellow] — could not parse {cfg_path}"
        )
        return
    cfg.setdefault("mcp", {})["witan"] = _mcp_entry(author)
    _write_json(cfg_path, cfg, dry_run)
    console.print(f"  [green]config.json[/green] → {cfg_path}")


# ── Kilo Code ─────────────────────────────────────────────────────────────────


def install_kilo(pkg_dir: Path, author: str, dry_run: bool) -> None:
    # Kilo Code is a VS Code extension; MCP servers are stored in VS Code's
    # user settings.json under the "kilocode.mcpServers" key.
    settings_path = _vscode_user_dir() / "settings.json"
    settings = _load_json(settings_path)
    if settings is None:
        console.print(
            f"  [yellow]skip settings.json[/yellow] — could not parse {settings_path}; add witan manually"
        )
        return
    settings.setdefault("kilocode.mcpServers", {})["witan"] = _mcp_entry(author)
    _write_json(settings_path, settings, dry_run)
    console.print(f"  [green]settings.json[/green] → {settings_path}")


# ── Auto-detection for --agent all ───────────────────────────────────────────


def is_pi_installed() -> bool:
    return (Path.home() / ".pi").is_dir()


def is_copilot_installed() -> bool:
    return _vscode_user_dir().is_dir()


def is_opencode_installed() -> bool:
    return (Path.home() / ".config" / "opencode").is_dir()


def is_kilo_installed() -> bool:
    # Kilo uses VS Code; presence of the global storage dir is a reasonable proxy.
    kilo_storage = _vscode_user_dir().parent / "globalStorage" / "kilocode.kilo-code"
    return kilo_storage.is_dir()
