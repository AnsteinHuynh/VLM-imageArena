"""
VLM-imageArena — judge server launcher.
Version: 1.5.0 (binary auto-resolution + flag-compat shim)

Starts llama-server with the vision model using the JUDGE_* settings from
config.py, waits for the health endpoint, then keeps the console attached
until the server exits (Ctrl+C stops both). If a server is already healthy
on the port, it exits without touching it.

v1.5.0: cross-platform era.
- server binary resolved via src/server_resolve.py: config_local.py override
  -> llamacpp/INSTALL.json (written by install_llamacpp.py) -> llamacpp/ scan
  -> PATH. No hardcoded exe path; Linux/macOS work the same way.
- flag-compat shim: probes `llama-server --help` and filters the tiered
  config.JUDGE_SERVER_FLAGS catalog — unsupported "tuned"/"optional" flags
  are dropped with a message, unsupported "critical" flags abort the launch
  (silently losing --jinja would corrupt multimodal votes, gotcha 1).
- --threads* default to os.cpu_count() (config.LLAMA_ARG_THREADS override);
  was hardcoded 8 for the old 35B box.
v1.4.1: _preflight() checks JUDGE_SERVER_EXE / LLAMA_ARG_MODEL / LLAMA_ARG_MMPROJ
BEFORE launching and prints setup instructions when they are placeholders or
missing (first-run friendliness; config ships neutral placeholders).
v1.3.0: --no-webui now conditional on config.JUDGE_WEBUI (default True since
2026-08-14 — the built-in web UI at http://127.0.0.1:9932 is handy for
personal judge testing before arena runs).
"""

VERSION = "1.5.0"

import os
import re
import subprocess
import sys
import time
from pathlib import Path

from . import config, judge
from .server_resolve import resolve_judge_server


def _threads():
    """Worker threads: config override, else every logical core."""
    n = getattr(config, "LLAMA_ARG_THREADS", None) or os.cpu_count() or 8
    return str(max(1, int(n)))


def _probe_supported_flags(exe):
    """Set of --flags the binary lists in --help, or None when the probe
    itself fails (then every configured flag is kept, with a warning)."""
    try:
        r = subprocess.run([str(exe), "--help"], capture_output=True,
                           text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired) as e:
        print(f"[judge_server] WARNING: could not probe --help ({e}); "
              f"using all configured flags")
        return None
    text = (r.stdout or "") + (r.stderr or "")
    flags = set(re.findall(r"(?<![\w-])(--[a-z0-9][a-z0-9-]*)", text))
    if not flags:
        print("[judge_server] WARNING: --help parsed empty; "
              "using all configured flags")
        return None
    return flags


def _build_cmd(exe, supported):
    """The llama-server command line. Structural flags are always emitted;
    catalog flags are filtered by tier against `supported` (None = probe
    failed, keep everything). Returns None when a critical flag is missing."""
    cmd = [
        str(exe),
        "--model", str(config.LLAMA_ARG_MODEL),
        "--mmproj", str(config.LLAMA_ARG_MMPROJ),
        "--port", str(config.LLAMA_ARG_PORT),
        "--threads", _threads(),
        "--threads-batch", _threads(),
        "--threads-http", _threads(),
        "--parallel", str(config.LLAMA_ARG_N_PARALLEL),
    ]
    for flag, value, tier in config.JUDGE_SERVER_FLAGS:
        if supported is not None and flag not in supported:
            if tier == "critical":
                print(f"[judge_server] ERROR: {flag} is marked critical but "
                      f"this llama-server build does not list it in --help.")
                print("[judge_server] The arena protocol depends on it "
                      "(multimodal media markers / vote contract). Update "
                      "llama.cpp, or consciously lower its tier in "
                      "config.JUDGE_SERVER_FLAGS.")
                return None
            label = "WARNING" if tier == "tuned" else "note"
            print(f"[judge_server] {label}: dropping unsupported flag "
                  f"{flag} ({tier})")
            continue
        cmd.append(flag)
        if value is not None:
            cmd.append(str(value))
    if config.LLAMA_ARG_IMAGE_MAX_TOKENS:
        cmd += ["--image-max-tokens", str(config.LLAMA_ARG_IMAGE_MAX_TOKENS)]
    if not config.JUDGE_WEBUI:
        cmd += ["--no-webui"]  # headless (arena-only; WebUI off)
    return cmd


def _safe_terminate(proc):
    """Best-effort kill; ignore errors if the process already exited."""
    try:
        proc.terminate()
    except (OSError, PermissionError):
        pass


def _preflight():
    """Resolve the server binary + model/mmproj paths before launching.

    Returns the exe Path, or None after printing what's missing.
    """
    exe, source, warning = resolve_judge_server()
    if warning:
        print(f"[judge_server] {warning}")
    ok = True
    if exe is None:
        print("[judge_server] no llama-server binary found.")
        print("[judge_server] Fix, pick one:")
        print("[judge_server]   1. install.bat -- auto-download into llamacpp\\")
        print("[judge_server]      (Linux/macOS: python src/install_llamacpp.py)")
        print("[judge_server]   2. set JUDGE_SERVER_EXE in src/config_local.py")
        print("[judge_server]   3. put llama-server on PATH")
        ok = False
    else:
        print(f"[judge_server] server binary: {exe}  (via {source})")
    checks = [
        ("LLAMA_ARG_MODEL", config.LLAMA_ARG_MODEL),
        ("LLAMA_ARG_MMPROJ", config.LLAMA_ARG_MMPROJ),
    ]
    for label, path in checks:
        p = Path(path)
        if "path\\to" in str(path) or "path/to" in str(path):
            print(f"[judge_server] {label} is still the placeholder: {path}")
            ok = False
        elif not p.exists():
            print(f"[judge_server] {label} not found: {path}")
            ok = False
    if not ok:
        print("[judge_server] Setup: run install.bat for the server binary, "
              "then point LLAMA_ARG_MODEL / LLAMA_ARG_MMPROJ in "
              "src/config_local.py at your GGUF files (main model + mmproj "
              "vision encoder).")
    return exe if ok else None


def main():
    if judge.server_healthy():
        print(f"[judge_server] llama-server already healthy at {config.SERVER_HEALTH_URL}")
        print("[judge_server] nothing to do — exiting")
        return 0

    exe = _preflight()
    if exe is None:
        return 1

    supported = _probe_supported_flags(exe)
    cmd = _build_cmd(exe, supported)
    if cmd is None:
        return 2

    print("[judge_server] launching:", " ".join(f'"{c}"' for c in cmd))
    proc = subprocess.Popen(cmd)  # inherits this console -> output stays visible

    print(f"[judge_server] waiting for {config.SERVER_HEALTH_URL} "
          f"(timeout {config.JUDGE_READY_TIMEOUT}s)...")
    deadline = time.time() + config.JUDGE_READY_TIMEOUT
    while time.time() < deadline:
        if proc.poll() is not None:
            print(f"[judge_server] ERROR: llama-server exited early with code {proc.returncode}")
            return proc.returncode
        if judge.server_healthy():
            print(f"[judge_server] READY at {config.SERVER_HEALTH_URL} (pid {proc.pid})")
            print("[judge_server] console stays attached — Ctrl+C to stop the server")
            break
        time.sleep(2)
    else:
        print(f"[judge_server] ERROR: server not healthy within {config.JUDGE_READY_TIMEOUT}s")
        _safe_terminate(proc)
        return 1

    try:
        proc.wait()
    except KeyboardInterrupt:
        print("\n[judge_server] Ctrl+C — stopping llama-server...")
        _safe_terminate(proc)
        proc.wait()
        print("[judge_server] stopped")
        return 1
    print(f"[judge_server] llama-server exited with code {proc.returncode}")
    return proc.returncode


if __name__ == "__main__":
    sys.exit(main())
