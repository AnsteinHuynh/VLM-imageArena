"""
VLM-imageArena -- judge-server binary resolution.
Version: 1.0.0

Finds a llama-server binary without hand-editing paths. Resolution order
(first hit wins):

1. config.JUDGE_SERVER_EXE -- the explicit config_local.py override; wins
   whenever it exists (relative paths are repo-root-relative, the same
   convention the *.bat launchers use when they cd to the repo root).
2. llamacpp/INSTALL.json -- the manifest written by install_llamacpp.py;
   "server_rel" is the binary's path inside llamacpp/ (POSIX-style, may be
   nested on Linux/macOS tarballs).
3. Scan llamacpp/ for llama-server(.exe).
4. shutil.which("llama-server") -- whatever is on PATH.

Returns (exe_path, source_label, warning). exe_path is None when nothing
resolves; warning is a human-readable note when an explicit path was set
but missing (we fell through instead of failing outright).
"""

VERSION = "1.0.0"

import json
import os
import shutil
from pathlib import Path

from . import config

_REPO_ROOT = Path(config.__file__).resolve().parents[1]
_LLAMACPP_DIR = _REPO_ROOT / "llamacpp"
_INSTALL_JSON = _LLAMACPP_DIR / "INSTALL.json"

_PLACEHOLDER_HINTS = ("path\\to", "path/to")


def _binary_names():
    """Likely server binary names, platform order first."""
    if os.name == "nt":
        return ("llama-server.exe", "llama-server")
    return ("llama-server", "llama-server.exe")


def _normalize(candidate):
    """Resolve a configured path; relative = repo-root-relative."""
    p = Path(candidate)
    if not p.is_absolute():
        p = _REPO_ROOT / p
    return p


def _from_explicit():
    """config_local.py override. Returns (path|None, warning|None)."""
    raw = str(config.JUDGE_SERVER_EXE)
    if any(h in raw for h in _PLACEHOLDER_HINTS):
        return None, None  # shipped placeholder -- silently skip
    p = _normalize(raw)
    if p.exists():
        return p, None
    return None, (f"config_local.py JUDGE_SERVER_EXE is set but missing: "
                  f"{p} -- falling back to auto-detection")


def _from_manifest():
    """llamacpp/INSTALL.json written by install_llamacpp.py."""
    if not _INSTALL_JSON.exists():
        return None, None
    try:
        info = json.loads(_INSTALL_JSON.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None, None
    rel = info.get("server_rel")
    candidates = []
    if rel:
        candidates.append(_LLAMACPP_DIR / Path(rel))
    for name in _binary_names():
        candidates.append(_LLAMACPP_DIR / name)
    for p in candidates:
        if p.exists():
            return p, None
    return None, None


def _from_scan():
    """Direct scan of llamacpp/ (root-level binaries only)."""
    for name in _binary_names():
        p = _LLAMACPP_DIR / name
        if p.exists():
            return p, None
    return None, None


def _from_path():
    """Last resort: llama-server on PATH."""
    for name in _binary_names():
        hit = shutil.which(name)
        if hit:
            return Path(hit), None
    return None, None


def resolve_judge_server():
    """Returns (exe_path | None, source_label, warning | None)."""
    finders = (
        (_from_explicit, "config_local.py"),
        (_from_manifest, "llamacpp/INSTALL.json"),
        (_from_scan, "llamacpp/ scan"),
        (_from_path, "PATH"),
    )
    warning = None
    for finder, label in finders:
        hit, warn = finder()
        if warn:
            warning = warn  # keep the first warning, keep searching
        if hit:
            return hit, label, warning
    return None, "", warning
