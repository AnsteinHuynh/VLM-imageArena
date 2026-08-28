"""
VLM-imageArena -- llama.cpp installer / updater.
Version: 1.0.0

Downloads the newest ggml-org/llama.cpp Windows x64 build (llama-server +
DLLs) into llamacpp\\ inside the repo and wires src/config_local.py's
JUDGE_SERVER_EXE to it (relative path; all *.bat cd to the repo root).
Models and mmproj are NOT downloaded -- they stay manual (see AGENTS.md:
llama.cpp releases ship no GGUFs).

Usage:
    install.bat                                  interactive menu
    python src\\install_llamacpp.py --dry-run     show plan, download nothing
    python src\\install_llamacpp.py --flavor vulkan
    python src\\install_llamacpp.py --update      (what update.bat calls)
    python src\\install_llamacpp.py --no-config   skip config_local.py wiring

Probing facts baked in (verified 2026-08-28 against the live API):
- GitHub /releases/latest is WRONG for ggml-org/llama.cpp: every b<NNNN>
  build release is prerelease=true, so "latest" resolves to an ancient
  stable tag (v0.3.0, one asset, no binaries). We list /releases and pick
  the newest tag matching ^b\\d+$ instead.
- CUDA flavors ship TWO zips: the main build plus cudart-llama-bin-win-
  cuda-<ver>-x64.zip (the CUDA runtime DLLs). Both extract into llamacpp\\.
- The network path to GitHub drops TLS handshakes intermittently on some
  machines, so every HTTP call retries with backoff. HTTPS_PROXY (or the
  --proxy flag) is honored for users behind a local proxy.
- Newer NVIDIA drivers label the header "CUDA UMD Version: 13.3" (the old
  "CUDA Version:" field is deprecated) -- the detector accepts both.
"""

VERSION = "1.0.0"

import argparse
import json
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import zipfile
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEST = REPO_ROOT / "llamacpp"
INSTALL_JSON = DEST / "INSTALL.json"
CONFIG_LOCAL = REPO_ROOT / "src" / "config_local.py"

GITHUB_RELEASES = "https://api.github.com/repos/ggml-org/llama.cpp/releases?per_page=10"
RELEASES_PAGE = "https://github.com/ggml-org/llama.cpp/releases"
USER_AGENT = "vlm-imageArena-installer/" + VERSION

MAIN_RE = re.compile(r"^llama-b(\d+)-bin-win-(.+)-x64\.zip$")
CUDART_RE = re.compile(r"^cudart-llama-bin-win-cuda-(\d+\.\d+)-x64\.zip$")
TAG_RE = re.compile(r"^b(\d+)$")
CUDA_FLAVOR_RE = re.compile(r"^cuda-(\d+)\.(\d+)$")

# _opener is set in main(): default opener honors HTTPS_PROXY env vars; if
# --proxy was passed we build an opener with an explicit ProxyHandler.
_opener = None


# --------------------------------------------------------------------------
# HTTP with retry (the TLS path to GitHub drops handshakes intermittently)
# --------------------------------------------------------------------------

def _transient(err):
    """Retry-worthy network error?"""
    if isinstance(err, urllib.error.HTTPError):
        return err.code >= 500 or err.code == 429
    return isinstance(err, (urllib.error.URLError, socket.timeout, OSError))


def _fetch(url, retries=4, backoff=2.0):
    """GET a URL, return the raw bytes. Retries on transient failures."""
    last = None
    for attempt in range(1, retries + 1):
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        try:
            with _opener.open(req, timeout=60) as resp:
                return resp.read()
        except (urllib.error.URLError, socket.timeout, OSError) as e:
            last = e
            if attempt < retries:
                wait = backoff * attempt
                print(f"  [net] attempt {attempt}/{retries} failed: {e} "
                      f"-- retrying in {wait:.0f}s")
                time.sleep(wait)
    raise RuntimeError(f"download failed after {retries} attempts: {last}")


def fetch_release():
    """Newest b<NNNN> release. NOT /releases/latest (all builds are
    prereleases; 'latest' points at an old stable tag with no binaries)."""
    data = json.loads(_fetch(GITHUB_RELEASES).decode("utf-8"))
    for rel in data:
        m = TAG_RE.match(rel.get("tag_name", ""))
        if m and rel.get("assets"):
            return {"tag": rel["tag_name"], "build": int(m.group(1)),
                    "published": rel.get("published_at", "?"),
                    "assets": rel["assets"]}
    raise RuntimeError(f"no b<NNNN> build release found -- check {RELEASES_PAGE}")


def download(url, dest, retries=3):
    """Stream a file with MB progress. Retries the WHOLE file on transient
    failures (simple, and restarts are cheap relative to a fresh fetch)."""
    last = None
    for attempt in range(1, retries + 1):
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        try:
            with _opener.open(req, timeout=60) as resp, open(dest, "wb") as f:
                total = int(resp.headers.get("Content-Length") or 0)
                got = 0
                while True:
                    chunk = resp.read(1024 * 1024)
                    if not chunk:
                        break
                    f.write(chunk)
                    got += len(chunk)
                    if total:
                        sys.stdout.write(f"\r    {got / 1048576:8.1f} / "
                                         f"{total / 1048576:.1f} MB")
                    else:
                        sys.stdout.write(f"\r    {got / 1048576:8.1f} MB")
                    sys.stdout.flush()
            print()  # newline after the progress line
            return
        except (urllib.error.URLError, socket.timeout, OSError) as e:
            last = e
            print()
            if attempt < retries:
                wait = 5.0 * attempt
                print(f"  [net] download attempt {attempt}/{retries} failed: "
                      f"{e} -- retrying in {wait:.0f}s")
                time.sleep(wait)
    raise RuntimeError(f"download failed after {retries} attempts: {last}")


# --------------------------------------------------------------------------
# Flavor catalogue
# --------------------------------------------------------------------------

def flavor_catalog(assets):
    """Map x64 windows flavors -> {'main': asset, 'cudart': asset|None}."""
    cats, cudarts = {}, {}
    for a in assets:
        name = a["name"]
        m = MAIN_RE.match(name)
        if m:
            cats[m.group(2)] = {"main": a}
            continue
        m = CUDART_RE.match(name)
        if m:
            cudarts[m.group(1)] = a
    for flavor, cat in cats.items():
        m = CUDA_FLAVOR_RE.match(flavor)
        if m:
            cat["cudart"] = cudarts.get(m.group(1))
    return cats


def _flavor_sort_key(name):
    """Menu order: cuda builds newest-first, then vulkan/sycl/rocm/openvino,
    cpu, then anything else alphabetically."""
    m = CUDA_FLAVOR_RE.match(name)
    if m:
        return (0, -int(m.group(1)), -int(m.group(2)), name)
    pref = {"vulkan": 1, "sycl": 2, "rocm": 3, "openvino": 4, "cpu": 5}
    for prefix, rank in pref.items():
        if name.startswith(prefix):
            return (rank, 0, 0, name)
    return (9, 0, 0, name)


def detect_cuda_driver():
    """(major, minor) of the driver's CUDA support, or None.

    Accepts both header styles:
      old:  | CUDA Version: 12.8 ...
      new:  KMD Version: 610.62   CUDA UMD Version: 13.3
    (the old field is deprecated and disappears in CUDA 14.0).
    """
    exe = shutil.which("nvidia-smi")
    if not exe:
        return None
    outputs = []
    for args in ([exe], [exe, "-q"]):
        try:
            r = subprocess.run(args, capture_output=True, text=True,
                               timeout=20)
            outputs.append(r.stdout or "")
        except OSError:
            return None
    for out in outputs:
        m = re.search(r"CUDA(?:\s+UMD)?\s*Version\s*[:\s]\s*(\d+)\.(\d+)", out)
        if m:
            return (int(m.group(1)), int(m.group(2)))
    return None


def recommend_flavor(catalog, driver):
    """Newest cuda-<X.Y> build the driver supports, else vulkan, else cpu."""
    if driver:
        best = None
        for name in catalog:
            m = CUDA_FLAVOR_RE.match(name)
            if m and (int(m.group(1)), int(m.group(2))) <= driver:
                key = (int(m.group(1)), int(m.group(2)))
                if best is None or key > best:
                    best = key
        if best:
            return f"cuda-{best[0]}.{best[1]}"
    if "vulkan" in catalog:
        return "vulkan"
    return sorted(catalog, key=_flavor_sort_key)[0]


def choose_flavor_interactive(catalog, recommended):
    """Numbered menu; Enter accepts the auto-detected recommendation."""
    names = sorted(catalog, key=_flavor_sort_key)
    print()
    print("Available llama.cpp builds (Windows x64):")
    for i, name in enumerate(names, 1):
        cat = catalog[name]
        size = cat["main"].get("size", 0)
        extra = ""
        if cat.get("cudart"):
            size += cat["cudart"].get("size", 0)
            extra = "  (incl. CUDA runtime DLLs)"
        mark = "   <-- recommended for this machine" if name == recommended else ""
        print(f"  {i}. {name:<22} ~{size / 1048576:.0f} MB download{extra}{mark}")
    print()
    while True:
        raw = input(f"Choose [1-{len(names)}] or flavor name "
                    f"(Enter = {recommended}): ").strip().lower()
        if not raw:
            return recommended
        if raw.isdigit() and 1 <= int(raw) <= len(names):
            return names[int(raw) - 1]
        for name in names:
            if name.lower() == raw:
                return name
        print(f"  [install] '{raw}' is not one of: {', '.join(names)}")


# --------------------------------------------------------------------------
# Install / update
# --------------------------------------------------------------------------

def current_install():
    """Installed build info from INSTALL.json, or None."""
    if not INSTALL_JSON.exists():
        return None
    try:
        info = json.loads(INSTALL_JSON.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not (DEST / "llama-server.exe").exists():
        return None
    return info


def server_version():
    """First version line of `llama-server.exe --version`.

    The build prints to STDERR (verified b10679), so merge both streams.
    """
    exe = DEST / "llama-server.exe"
    try:
        r = subprocess.run([str(exe), "--version"], capture_output=True,
                           text=True, timeout=20)
        lines = (r.stdout or "").strip().splitlines() or \
                (r.stderr or "").strip().splitlines() or [""]
        return lines[0] or "(no version output)"
    except (OSError, subprocess.TimeoutExpired):
        return "(version lookup failed)"


def _safe_extract(zip_path, dest):
    """Extract with a zip-slip guard (no absolute paths / .. members)."""
    with zipfile.ZipFile(zip_path) as z:
        for name in z.namelist():
            p = Path(name)
            if p.is_absolute() or ".." in p.parts or (len(name) > 1
                                                      and name[1] == ":"):
                raise RuntimeError(f"unsafe path in {zip_path.name}: {name}")
        z.extractall(dest)


def install_build(rel, flavor, catalog, dry_run):
    """Download + extract the flavor's zips into llamacpp\\, write manifest."""
    cat = catalog[flavor]
    picks = [cat["main"]]
    if cat.get("cudart"):
        picks.append(cat["cudart"])
    total = sum(a.get("size", 0) for a in picks)
    print(f"\n[install] llama.cpp {rel['tag']}, flavor '{flavor}' "
          f"({len(picks)} zip(s), ~{total / 1048576:.0f} MB total)")
    for a in picks:
        print(f"    {a['name']}  ({a.get('size', 0) / 1048576:.0f} MB)")
    if dry_run:
        print("[install] dry-run -- nothing downloaded, nothing written.")
        return False

    DEST.mkdir(exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix=f"llamacpp-dl-{rel['tag']}-"))
    try:
        for a in picks:
            print(f"  [dl] {a['name']}")
            target = tmp / a["name"]
            download(a["browser_download_url"], target)
            _safe_extract(target, DEST)  # verify + extract immediately
            target.unlink()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    exe = DEST / "llama-server.exe"
    if not exe.exists():
        raise RuntimeError("llama-server.exe not found after extraction -- "
                           "asset layout changed? See " + RELEASES_PAGE)
    info = {"tag": rel["tag"], "build": rel["build"], "flavor": flavor,
            "installed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "assets": [a["name"] for a in picks]}
    INSTALL_JSON.write_text(json.dumps(info, indent=2) + "\n",
                            encoding="utf-8")
    print(f"[install] installed: {rel['tag']} ({flavor}) -> {DEST}")
    print(f"[install] {server_version()}")
    return True


# --------------------------------------------------------------------------
# config_local.py wiring (relative path; bats always cd to the repo root)
# --------------------------------------------------------------------------

CONFIG_TEMPLATE = '''r"""Local machine overrides for src/config.py -- created by install.bat.

JUDGE_SERVER_EXE below points at the llama.cpp build installed into
llamacpp\\ (relative on purpose: every *.bat cd's to the repo root first,
so it survives moving the repo folder).

STILL MANUAL: the model + mmproj GGUFs (llama.cpp ships no models). Set
LLAMA_ARG_MODEL and LLAMA_ARG_MMPROJ below, then run start_judge.bat --
its preflight will tell you exactly what is missing.
"""

JUDGE_SERVER_EXE = r"llamacpp\\llama-server.exe"
LLAMA_ARG_MODEL = r"C:\\path\\to\\model.gguf"
LLAMA_ARG_MMPROJ = r"C:\\path\\to\\mmproj.gguf"
'''

PLACEHOLDER_HINTS = ("path\\to", "path/to")


def wire_config_local(dry_run):
    """Point JUDGE_SERVER_EXE at llamacpp\\llama-server.exe.

    - no config_local.py  -> create a minimal one
    - placeholder line    -> patch just that line in place
    - real path set       -> report and leave untouched (never clobber)
    """
    if not CONFIG_LOCAL.exists():
        if dry_run:
            print("[config] would CREATE src/config_local.py with "
                  "JUDGE_SERVER_EXE = llamacpp\\llama-server.exe")
            return
        CONFIG_LOCAL.write_text(CONFIG_TEMPLATE, encoding="utf-8")
        print("[config] created src/config_local.py "
              "(JUDGE_SERVER_EXE -> llamacpp\\llama-server.exe)")
        return

    text = CONFIG_LOCAL.read_text(encoding="utf-8")
    line_re = re.compile(r"(?m)^(\s*)JUDGE_SERVER_EXE\s*=\s*(.+?)\s*$")
    m = line_re.search(text)
    new_line = 'JUDGE_SERVER_EXE = r"llamacpp\\llama-server.exe"'
    if not m:
        if dry_run:
            print("[config] would APPEND the JUDGE_SERVER_EXE override")
            return
        sep = "" if text.endswith("\n") else "\n"
        CONFIG_LOCAL.write_text(
            text + sep + "\n# --- added by install.bat ---\n" + new_line + "\n",
            encoding="utf-8")
        print("[config] appended JUDGE_SERVER_EXE override to "
              "src/config_local.py")
        return
    if any(h in m.group(2) for h in PLACEHOLDER_HINTS):
        if dry_run:
            print(f"[config] would PATCH placeholder line: {m.group(0).strip()}")
            return
        replacement = m.group(1) + new_line
        CONFIG_LOCAL.write_text(text[:m.start()] + replacement + text[m.end():],
                                encoding="utf-8")
        print("[config] patched placeholder JUDGE_SERVER_EXE in "
              "src/config_local.py")
        return
    print(f"[config] JUDGE_SERVER_EXE already set to a real path -- "
          f"left untouched:\n           {m.group(2).strip()}")


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------

def report_current():
    info = current_install()
    if info is None:
        return False
    print(f"[install] llama.cpp already installed: "
          f"{info.get('tag', '?')} (flavor {info.get('flavor', '?')}, "
          f"installed {info.get('installed_at', '?')})")
    print(f"[install] {server_version()}")
    print("[install] nothing to do -- rerun with --update to fetch a newer "
          "build, or delete llamacpp\\ to reinstall.")
    return True


def main():
    global _opener
    ap = argparse.ArgumentParser(
        description="Install/update llama.cpp (llama-server) for "
                    "VLM-imageArena.")
    ap.add_argument("--flavor", metavar="NAME",
                    help="windows x64 flavor, e.g. cuda-13.3, vulkan, cpu "
                         "(skips the interactive menu)")
    ap.add_argument("--update", action="store_true",
                    help="fetch a newer build if one exists (no menu; "
                         "reuses the installed flavor)")
    ap.add_argument("--dry-run", action="store_true",
                    help="show the plan and download nothing")
    ap.add_argument("--no-config", action="store_true",
                    help="do not touch src/config_local.py")
    ap.add_argument("--proxy", metavar="URL",
                    help="HTTP(S) proxy, e.g. http://127.0.0.1:7890 "
                         "(overrides HTTPS_PROXY)")
    args = ap.parse_args()

    if args.proxy:
        _opener = urllib.request.build_opener(urllib.request.ProxyHandler(
            {"http": args.proxy, "https": args.proxy}))
    else:
        _opener = urllib.request.build_opener()  # honors HTTPS_PROXY env

    print(f"[install] VLM-imageArena llama.cpp installer v{VERSION}")

    # 1. Locate the newest build release (with retries).
    try:
        rel = fetch_release()
    except RuntimeError as e:
        print(f"[install] ERROR: cannot reach GitHub: {e}")
        print(f"[install] Manual fallback: open {RELEASES_PAGE} and grab the "
              f"newest llama-bNNNN-bin-win-<flavor>-x64.zip yourself "
              f"(+ the cudart zip for cuda flavors), extract into llamacpp\\.")
        print("[install] If you browse through a local proxy, set it for the "
              "console too, e.g.:  set HTTPS_PROXY=http://127.0.0.1:7890")
        return 1
    print(f"[install] latest build release: {rel['tag']} "
          f"(published {rel['published']})")

    catalog = flavor_catalog(rel["assets"])
    if not catalog:
        print("[install] ERROR: no windows x64 zips in this release -- "
              f"check {RELEASES_PAGE}")
        return 1

    # 2. Mode: --update, report-and-exit, or fresh install.
    if args.update:
        info = current_install()
        if info is None:
            print("[update] llama.cpp is not installed yet -- run "
                  "install.bat first.")
            return 1
        if info.get("build") == rel["build"]:
            print(f"[update] already up to date ({rel['tag']})")
            return 0
        flavor = info.get("flavor")
        if flavor not in catalog:
            print(f"[update] installed flavor '{flavor}' no longer exists in "
                  f"{rel['tag']}; run install.bat to pick a new one.")
            return 1
        install_build(rel, flavor, catalog, args.dry_run)
        return 0

    if report_current():
        return 0

    driver = detect_cuda_driver()
    if driver:
        print(f"[install] NVIDIA GPU detected -- driver CUDA {driver[0]}.{driver[1]}")
    else:
        print("[install] no NVIDIA driver detected -- Vulkan/CPU build it is.")
    recommended = recommend_flavor(catalog, driver)

    flavor = args.flavor
    if flavor:
        if flavor not in catalog:
            print(f"[install] ERROR: flavor '{flavor}' not in {rel['tag']}. "
                  f"Available: {', '.join(sorted(catalog, key=_flavor_sort_key))}")
            return 1
    else:
        if args.dry_run:
            print(f"[install] dry-run: would recommend '{recommended}' "
                  f"(use --flavor to force)")
            for name in sorted(catalog, key=_flavor_sort_key):
                print(f"    - {name}")
            return 0
        if not sys.stdin.isatty():
            print("[install] ERROR: no interactive console -- pass --flavor "
                  f"(available: {', '.join(sorted(catalog, key=_flavor_sort_key))})")
            return 1
        flavor = choose_flavor_interactive(catalog, recommended)

    install_build(rel, flavor, catalog, args.dry_run)
    if not args.dry_run:
        if not args.no_config:
            wire_config_local(dry_run=False)
        print()
        print("[install] Next steps:")
        print("  1. point LLAMA_ARG_MODEL / LLAMA_ARG_MMPROJ in "
              "src/config_local.py at your GGUFs (models are NOT downloaded)")
        print("  2. start_judge.bat   (preflight + health wait)")
        print("  3. start_arena.bat   (dashboard at http://127.0.0.1:8843)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
