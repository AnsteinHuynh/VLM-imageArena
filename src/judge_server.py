"""
VLM-imageArena — judge server launcher.
Version: 1.4.1 (path pre-flight with setup instructions)

Starts llama-server with the vision model using the JUDGE_* settings from
config.py, waits for the health endpoint, then keeps the console attached
until the server exits (Ctrl+C stops both). If a server is already healthy
on the port, it exits without touching it.

v1.4.1: _preflight() checks JUDGE_SERVER_EXE / LLAMA_ARG_MODEL / LLAMA_ARG_MMPROJ
BEFORE launching and prints setup instructions when they are placeholders or
missing (first-run friendliness; config ships neutral placeholders).
v1.3.0: --no-webui now conditional on config.JUDGE_WEBUI (default True since
2026-08-14 — the built-in web UI at http://127.0.0.1:9932 is handy for
personal judge testing before arena runs).
"""

VERSION = "1.4.1"

import subprocess
import sys
import time

from . import config, judge


def _build_cmd():
    cmd = [
        config.JUDGE_SERVER_EXE,
        #https://github.com/ggml-org/llama.cpp/tree/master/tools/server
        "--model", config.LLAMA_ARG_MODEL,
        "--mmproj", config.LLAMA_ARG_MMPROJ,

        "--ctx-size", str("6144"), #12288 for qwen38-q5m-parallel4 old  98304
        "--n-gpu-layers", str("99"),

        "--port", str(config.LLAMA_ARG_PORT),
        "--threads",str("8"),
        "--threads-batch",str("8"),
        "--poll-batch",str("1"),

        "--batch-size", str("4096"), #2048
        "--ubatch-size", str("4096"), #2048
        "--keep",str("-1"),
        "--threads-batch-draft",str("8"),
        "--threads-draft",str("8"),
        "--threads-http",str("8"),
        "--cache-ram",str("8192"),
        "--cache-prompt",
        "--cache-reuse",str("2048"), #was 1024
        "--props",   #Default was off
        "--metrics", #Default was off
        "--slots",
        "--jinja",

        "--parallel", str(config.LLAMA_ARG_N_PARALLEL),
        "--spec-type", str("none"), # {'draft-mtp','none'}
        "--reasoning", str("off"),   # production: reflex mode. (2026-08-15
            # experiments: reasoning ON requires JUDGE_USE_GRAMMAR=False and a
            # thinking budget >= ~128 tokens — and it burns latency for no
            # measurable vote-quality gain on this judge.)
        #"--log-disable",
        "--mtmd-batch-max-tokens", str("2048"),
        "--no-warmup", #Maybe this will be groundbreaking speed!
        "--flash-attn", "on",
    ]
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
    """Friendly first-run check: are the machine-specific paths set?

    config.py ships neutral placeholders; the real values live in the
    gitignored src/config_local.py (copy from src/config_local.example.py).
    Returns True when everything resolves.
    """
    from pathlib import Path

    checks = [
        ("JUDGE_SERVER_EXE", config.JUDGE_SERVER_EXE),
        ("LLAMA_ARG_MODEL", config.LLAMA_ARG_MODEL),
        ("LLAMA_ARG_MMPROJ", config.LLAMA_ARG_MMPROJ),
    ]
    ok = True
    for label, path in checks:
        p = Path(path)
        if "path\\to" in str(path) or "path/to" in str(path):
            print(f"[judge_server] {label} is still the placeholder: {path}")
            ok = False
        elif not p.exists():
            print(f"[judge_server] {label} not found: {path}")
            ok = False
    if not ok:
        print("[judge_server] Setup: copy src/config_local.example.py to "
              "src/config_local.py and point JUDGE_SERVER_EXE / "
              "LLAMA_ARG_MODEL / LLAMA_ARG_MMPROJ at your llama-server build "
              "+ GGUF files (main model + mmproj vision encoder).")
    return ok


def main():
    if judge.server_healthy():
        print(f"[judge_server] llama-server already healthy at {config.SERVER_HEALTH_URL}")
        print("[judge_server] nothing to do — exiting")
        return 0

    if not _preflight():
        return 1

    cmd = _build_cmd()
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
