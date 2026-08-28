"""
VLM-imageArena -- llama.cpp installer / updater (Windows / Linux / macOS).
Version: 2.0.0

Downloads a ggml-org/llama.cpp llama-server build into llamacpp\\ inside the
repo and wires src/config_local.py's JUDGE_SERVER_EXE to it. Models and
mmproj are NOT downloaded -- llama.cpp releases ship no GGUFs (see AGENTS.md).

v2.0.0 (cross-platform):
- asset parsing generalized: llama-b<build>-bin-<os>-<flavor>-<arch>.<ext>
  with the flavor segment OPTIONAL (llama-b10679-bin-macos-arm64.tar.gz)
- targets the running platform by default (win/*.zip, ubuntu+macos/*.tar.gz),
  overridable with --os / --arch
- archives: zip on Windows, tar.gz elsewhere; zip-slip guards on both
- finds the actual llama-server binary in the extracted tree (Linux/macOS
  tarballs nest it), chmod +x on POSIX, records its relative path in
  INSTALL.json as "server_rel" -- src/server_resolve.py consumes that, so
  nothing ever guesses layouts
- GPU detection: nvidia-smi -> CUDA builds; rocminfo/rocm-smi -> ROCm (Linux)
  else Vulkan; macOS = Metal (no detection needed); fallback Vulkan, then CPU

Usage:
    install.bat                                   interactive menu (Windows)
    python src\\install_llamacpp.py --dry-run      show plan, download nothing
    python src\\install_llamacpp.py --flavor vulkan
    python src\\install_llamacpp.py --update       (what update.bat calls)
    python src\\install_llamacpp.py --no-config    skip config_local.py wiring
    python src\\install_llamacpp.py --os ubuntu --arch x64   (cross-fetch)

Probing facts baked in (verified 2026-08-28 against the live API):
- GitHub /releases/latest is WRONG for ggml-org/llama.cpp: every b<NNNN>
  build release is prerelease=true, so "latest" resolves to an ancient
  stable tag (v0.3.0, one asset, no binaries). We list /releases and pick
  the newest tag matching ^b\\d+$ instead.
- Windows CUDA flavors ship TWO zips: the main build plus cudart-llama-bin-
  win-cuda-<ver>-x64.zip (CUDA runtime DLLs). Both extract into llamacpp\\.
  Linux releases ship NO CUDA build at all (CPU/Vulkan/ROCm/SYCL/OpenVINO);
  macOS builds are small and include Metal.
- The network path to GitHub drops TLS handshakes intermittently on some
  machines, so every HTTP call retries with backoff. HTTPS_PROXY (or the
  --proxy flag) is honored for users behind a local proxy.
- Newer NVIDIA drivers label the header "CUDA UMD Version: 13.3" (the old
  "CUDA Version:" field is deprecated) -- the detector accepts both.
"""

VERSION = "2.0.0"

import argparse
import json
import os
import platform
import re
import shutil
import socket
import subprocess
import sys
import tarfile
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

# llama-b10679-bin-win-cuda-12.4-x64.zip | llama-b10679-bin-macos-arm64.tar.gz
# The flavor segment is optional (macos/android/plain-ubuntu have none).
ASSET_RE = re.compile(
    r"^llama-b(?P<build>\d+)-bin-(?P<osname>win|ubuntu|macos|android)"
    r"(?:-(?P<flavor>.+?))?-(?P<arch>x64|arm64)\.(?P<ext>zip|tar\.gz)$")
CUDART_RE = re.compile(r"^cudart-llama-bin-win-cuda-(\d+\.\d+)-x64\.zip$")
TAG_RE = re.compile(r"^b(\d+)$")
CUDA_FLAVOR_RE = re.compile(r"^cuda-(\d+)\.(\d+)$")
ROCM_FLAVOR_RE = re.compile(r"^rocm-(\d+)\.(\d+)$")

# sys.platform / platform.machine() -> release-asset vocabulary
OS_KEY = {"win32": "win", "linux": "ubuntu", "darwin": "macos"}
ARCH_KEY = {"amd64": "x64", "x86_64": "x64", "arm64": "arm64", "aarch64": "arm64"}
# Flavor shown when the asset has none (plain CPU ubuntu build, macOS Metal).
DEFAULT_FLAVOR = {"win": "cpu", "ubuntu": "cpu", "macos": "metal"}

# _opener is set in main(): default opener honors HTTPS_PROXY env vars; if
# --proxy was passed we build an opener with an explicit ProxyHandler.
_opener = None


# --------------------------------------------------------------------------
# HTTP with retry (the TLS path to GitHub drops handshakes intermittently)
# --------------------------------------------------------------------------

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
# Flavor catalogue (per target OS + arch)
# --------------------------------------------------------------------------

def _default_flavor_name(os_key):
    return DEFAULT_FLAVOR.get(os_key, "default")


def flavor_catalog(assets, os_key, arch_key):
    """Map flavor name -> {'main': asset, 'cudart': asset|None, ...} for one
    platform. Flavor names come from the assets themselves (future-proof);
    flavor-less assets get the platform default name (cpu / metal)."""
    cudarts = {}
    for a in assets:
        mc = CUDART_RE.match(a["name"])
        if mc:
            # key by the full flavor name ("cuda-13.3") so the pairing below
            # is a direct lookup -- v1.0.0 looked up group(1) ("13" instead
            # of "13.3") and silently skipped the CUDA runtime DLLs.
            cudarts["cuda-" + mc.group(1)] = a
    cats = {}
    for a in assets:
        m = ASSET_RE.match(a["name"])
        if not m:
            continue
        if m.group("osname") != os_key or m.group("arch") != arch_key:
            continue
        flavor = m.group("flavor") or _default_flavor_name(os_key)
        cat = cats.setdefault(flavor, {
            "main": a, "cudart": None, "os": os_key, "arch": arch_key})
        if os_key == "win":
            cat["cudart"] = cudarts.get(flavor)
    return cats


def _flavor_sort_key(name):
    """Menu order: cuda newest-first, then rocm, vulkan, sycl, openvino,
    the platform default (cpu/metal), then anything else alphabetically."""
    m = CUDA_FLAVOR_RE.match(name)
    if m:
        return (0, -int(m.group(1)), -int(m.group(2)), name)
    m = ROCM_FLAVOR_RE.match(name)
    if m:
        return (1, -int(m.group(1)), -int(m.group(2)), name)
    pref = {"vulkan": 2, "sycl": 3, "openvino": 4}
    for prefix, rank in pref.items():
        if name.startswith(prefix):
            return (rank, 0, 0, name)
    return (5, 0, 0, name)


def _put_default_last_key(default_name):
    """Sort key tweak: platform default flavor (cpu/metal) goes last."""
    def key(name):
        base = _flavor_sort_key(name)
        if name == default_name:
            return (base[0], 9, 9, name)  # after every other same-rank entry
        return base
    return key


# --------------------------------------------------------------------------
# GPU / accelerator detection
# --------------------------------------------------------------------------

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


def detect_gpu():
    """('nvidia', (maj,min)) | ('amd', None) | ('apple', None) | (None, None)."""
    if shutil.which("nvidia-smi"):
        return ("nvidia", detect_cuda_driver())
    if shutil.which("rocminfo") or shutil.which("rocm-smi"):
        return ("amd", None)
    if sys.platform == "darwin":
        return ("apple", None)
    return (None, None)


def recommend_flavor(catalog, gpu, os_key):
    """GPU-aware default. NVIDIA -> newest supported CUDA build (Windows;
    Linux releases carry no CUDA build, so Vulkan there), AMD -> ROCm when
    ROCm tools exist on Linux else Vulkan, Apple -> the Metal build,
    unknown -> Vulkan then CPU. ALWAYS returns a catalog entry."""
    names = set(catalog)
    if not names:
        return None
    vendor, driver = gpu

    def pick(*prefixes):
        ranked = sorted((n for n in names
                         if any(n.startswith(p) for p in prefixes)),
                        key=_flavor_sort_key)
        return ranked[0] if ranked else None

    def fallback():
        """Vulkan -> platform default (cpu/metal) -> anything, in that order."""
        for hit in (pick("vulkan"), pick(_default_flavor_name(os_key))):
            if hit:
                return hit
        return sorted(names, key=_flavor_sort_key)[0]

    if vendor == "nvidia":
        if os_key == "win" and driver:
            best = None
            for name in names:
                m = CUDA_FLAVOR_RE.match(name)
                if m and (int(m.group(1)), int(m.group(2))) <= driver:
                    key = (int(m.group(1)), int(m.group(2)))
                    if best is None or key > best[0]:
                        best = (key, name)
            if best:
                return best[1]
        return fallback()  # e.g. NVIDIA on Linux: no CUDA builds exist there
    if vendor == "amd":
        if os_key != "win" and any(ROCM_FLAVOR_RE.match(n) for n in names):
            return pick("rocm")
        return fallback()
    if vendor == "apple":
        return pick(_default_flavor_name(os_key)) or fallback()
    return fallback()


def choose_flavor_interactive(catalog, recommended):
    """Numbered menu; Enter accepts the auto-detected recommendation."""
    first = next(iter(catalog.values()))
    default_name = _default_flavor_name(first["os"])
    names = sorted(catalog, key=_put_default_last_key(default_name))
    print()
    print(f"Available llama.cpp builds ({first['os']}-{first['arch']}):")
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

def _binary_names():
    if os.name == "nt":
        return ("llama-server.exe", "llama-server")
    return ("llama-server", "llama-server.exe")


def current_install():
    """Installed build info from INSTALL.json, or None (v1 manifests without
    server_rel are accepted; the root-level binary is then required)."""
    if not INSTALL_JSON.exists():
        return None
    try:
        info = json.loads(INSTALL_JSON.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if _server_binary(info) is None:
        return None
    return info


def _server_binary(info):
    """Where the installed server binary lives (Path) per the manifest."""
    rel = info.get("server_rel")
    if rel:
        p = DEST / Path(rel)
        if p.exists():
            return p
    for name in _binary_names():
        p = DEST / name
        if p.exists():
            return p
    return None


def server_version():
    """First version line of `llama-server --version`.

    The build prints to STDERR (verified b10679), so merge both streams.
    """
    exe = _server_binary(current_install() or {})
    if exe is None:
        return "(server binary not found)"
    try:
        r = subprocess.run([str(exe), "--version"], capture_output=True,
                           text=True, timeout=20)
        lines = (r.stdout or "").strip().splitlines() or \
                (r.stderr or "").strip().splitlines() or [""]
        return lines[0] or "(no version output)"
    except (OSError, subprocess.TimeoutExpired):
        return "(version lookup failed)"


def _guard_member_name(name):
    """Reject absolute paths, drive letters and .. traversal in archives."""
    p = Path(name)
    if p.is_absolute() or ".." in p.parts or (len(name) > 1 and name[1] == ":"):
        raise RuntimeError(f"unsafe path in archive: {name}")


def _safe_extract_zip(zip_path, dest):
    with zipfile.ZipFile(zip_path) as z:
        for name in z.namelist():
            _guard_member_name(name)
        z.extractall(dest)


def _safe_extract_tar(tar_path, dest):
    with tarfile.open(tar_path, "r:*") as t:
        for m in t.getmembers():
            _guard_member_name(m.name)
        try:
            t.extractall(dest, filter="data")  # py3.12+: blocks traversal
        except TypeError:
            t.extractall(dest)


def extract_archive(path, dest):
    if zipfile.is_zipfile(path):
        _safe_extract_zip(path, dest)
    elif tarfile.is_tarfile(path):
        _safe_extract_tar(path, dest)
    else:
        raise RuntimeError(f"unknown archive format: {path.name}")


def find_server_binary(dest):
    """Shortest-path llama-server(.exe) under dest (root beats nested)."""
    names = set(_binary_names())
    hits = []
    for root, _dirs, files in os.walk(dest):
        for f in files:
            if f in names:
                hits.append(Path(root) / f)
    if not hits:
        return None
    hits.sort(key=lambda p: (len(p.relative_to(dest).parts), str(p).lower()))
    return hits[0]


def install_build(rel, flavor, catalog, dry_run):
    """Download + extract the flavor's archives into llamacpp\\, locate the
    server binary, write the INSTALL.json manifest."""
    cat = catalog[flavor]
    picks = [cat["main"]]
    if cat.get("cudart"):
        picks.append(cat["cudart"])
    total = sum(a.get("size", 0) for a in picks)
    print(f"\n[install] llama.cpp {rel['tag']}, flavor '{flavor}' "
          f"({cat['os']}-{cat['arch']}, {len(picks)} archive(s), "
          f"~{total / 1048576:.0f} MB total)")
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
            extract_archive(target, DEST)  # verify + extract immediately
            target.unlink()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    exe = find_server_binary(DEST)
    if exe is None:
        raise RuntimeError("llama-server binary not found after extraction -- "
                           "asset layout changed? See " + RELEASES_PAGE)
    if os.name != "nt":
        try:
            exe.chmod(exe.stat().st_mode | 0o755)
        except OSError:
            pass
    server_rel = exe.relative_to(DEST).as_posix()
    info = {"tag": rel["tag"], "build": rel["build"], "flavor": flavor,
            "os": cat["os"], "arch": cat["arch"], "server_rel": server_rel,
            "installed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "assets": [a["name"] for a in picks]}
    INSTALL_JSON.write_text(json.dumps(info, indent=2) + "\n",
                            encoding="utf-8")
    print(f"[install] installed: {rel['tag']} ({flavor}) -> {DEST}")
    print(f"[install] binary: llamacpp/{server_rel}")
    print(f"[install] {server_version()}")
    return True


# --------------------------------------------------------------------------
# config_local.py wiring (relative path; bats always cd to the repo root)
# --------------------------------------------------------------------------

CONFIG_TEMPLATE = '''r"""Local machine overrides for src/config.py -- created by install.bat.

JUDGE_SERVER_EXE below points at the llama.cpp build installed into
llamacpp\\ (relative on purpose: every *.bat cd's to the repo root first,
and src/server_resolve.py interprets relative paths from the repo root).
On Linux/macOS the same layout works: llamacpp/llama-server.

STILL MANUAL: the model + mmproj GGUFs (llama.cpp ships no models). Set
LLAMA_ARG_MODEL and LLAMA_ARG_MMPROJ below, then run start_judge.bat --
its preflight will tell you exactly what is missing.
"""

JUDGE_SERVER_EXE = r"llamacpp\\llama-server.exe"
LLAMA_ARG_MODEL = r"C:\\path\\to\\model.gguf"
LLAMA_ARG_MMPROJ = r"C:\\path\\to\\mmproj.gguf"
'''

PLACEHOLDER_HINTS = ("path\\to", "path/to")


def wire_config_local(dry_run, server_name="llama-server.exe"):
    """Point JUDGE_SERVER_EXE at the installed binary.

    - no config_local.py  -> create a minimal one
    - placeholder line    -> patch just that line in place
    - real path set       -> report and leave untouched (never clobber)
    """
    target = f'llamacpp\\{server_name}' if os.name == "nt" \
        else f'llamacpp/{server_name}'
    new_line = f'JUDGE_SERVER_EXE = r"{target}"'
    if not CONFIG_LOCAL.exists():
        if dry_run:
            print(f"[config] would CREATE src/config_local.py with "
                  f"JUDGE_SERVER_EXE = {target}")
            return
        CONFIG_LOCAL.write_text(CONFIG_TEMPLATE, encoding="utf-8")
        print(f"[config] created src/config_local.py "
              f"(JUDGE_SERVER_EXE -> {target})")
        return

    text = CONFIG_LOCAL.read_text(encoding="utf-8")
    line_re = re.compile(r"(?m)^(\s*)JUDGE_SERVER_EXE\s*=\s*(.+?)\s*$")
    m = line_re.search(text)
    if not m:
        if dry_run:
            print("[config] would APPEND the JUDGE_SERVER_EXE override")
            return
        sep = "" if text.endswith("\n") else "\n"
        CONFIG_LOCAL.write_text(
            text + sep + "\n# --- added by install.bat ---\n" + new_line + "\n",
            encoding="utf-8")
        print(f"[config] appended JUDGE_SERVER_EXE override to "
              f"src/config_local.py")
        return
    if any(h in m.group(2) for h in PLACEHOLDER_HINTS):
        if dry_run:
            print(f"[config] would PATCH placeholder line: {m.group(0).strip()}")
            return
        replacement = m.group(1) + new_line
        CONFIG_LOCAL.write_text(text[:m.start()] + replacement + text[m.end():],
                                encoding="utf-8")
        print(f"[config] patched placeholder JUDGE_SERVER_EXE in "
              f"src/config_local.py")
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
    exe = _server_binary(info)
    print(f"[install] llama.cpp already installed: "
          f"{info.get('tag', '?')} (flavor {info.get('flavor', '?')}, "
          f"installed {info.get('installed_at', '?')})")
    print(f"[install] binary: {exe}")
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
                    help="build flavor for the target platform, e.g. "
                         "cuda-13.3 / vulkan / rocm-7.14 / cpu / metal "
                         "(skips the interactive menu)")
    ap.add_argument("--update", action="store_true",
                    help="fetch a newer build if one exists (no menu; "
                         "reuses the installed flavor/os/arch)")
    ap.add_argument("--dry-run", action="store_true",
                    help="show the plan and download nothing")
    ap.add_argument("--no-config", action="store_true",
                    help="do not touch src/config_local.py")
    ap.add_argument("--os", dest="os_key", choices=["win", "ubuntu", "macos"],
                    help="target OS (default: the running platform)")
    ap.add_argument("--arch", choices=["x64", "arm64"],
                    help="target CPU arch (default: the running platform)")
    ap.add_argument("--proxy", metavar="URL",
                    help="HTTP(S) proxy, e.g. http://127.0.0.1:7890 "
                         "(overrides HTTPS_PROXY)")
    args = ap.parse_args()

    if args.proxy:
        _opener = urllib.request.build_opener(urllib.request.ProxyHandler(
            {"http": args.proxy, "https": args.proxy}))
    else:
        _opener = urllib.request.build_opener()  # honors HTTPS_PROXY env

    os_key = args.os_key or OS_KEY.get(sys.platform)
    arch_key = args.arch or ARCH_KEY.get(platform.machine().lower())
    if not os_key or not arch_key:
        print(f"[install] ERROR: unsupported platform "
              f"({sys.platform} / {platform.machine()}); use --os and --arch.")
        return 1

    print(f"[install] VLM-imageArena llama.cpp installer v{VERSION} "
          f"(target: {os_key}-{arch_key})")

    # 1. Locate the newest build release (with retries).
    try:
        rel = fetch_release()
    except RuntimeError as e:
        print(f"[install] ERROR: cannot reach GitHub: {e}")
        print(f"[install] Manual fallback: open {RELEASES_PAGE} and grab the "
              f"newest llama-bNNNN-bin-... archive for your platform "
              f"(+ the cudart zip for Windows CUDA flavors), extract into "
              f"llamacpp\\.")
        print("[install] If you browse through a local proxy, set it for the "
              "console too, e.g.:  set HTTPS_PROXY=http://127.0.0.1:7890")
        return 1
    print(f"[install] latest build release: {rel['tag']} "
          f"(published {rel['published']})")

    catalog = flavor_catalog(rel["assets"], os_key, arch_key)
    if not catalog:
        print(f"[install] ERROR: no {os_key}-{arch_key} builds in this "
              f"release -- check {RELEASES_PAGE}")
        return 1

    # 2. Mode: --update, report-and-exit, or fresh install.
    if args.update:
        info = current_install()
        if info is None:
            print("[update] llama.cpp is not installed yet -- run "
                  "install.bat (or the installer) first.")
            return 1
        if info.get("build") == rel["build"]:
            print(f"[update] already up to date ({rel['tag']})")
            return 0
        flavor = info.get("flavor")
        if flavor not in catalog:
            print(f"[update] installed flavor '{flavor}' no longer exists in "
                  f"{rel['tag']}; rerun the installer to pick a new one.")
            return 1
        install_build(rel, flavor, catalog, args.dry_run)
        return 0

    if report_current():
        return 0

    gpu = detect_gpu()
    vendor = gpu[0]
    if vendor == "nvidia":
        maj, mn = gpu[1] or (0, 0)
        print(f"[install] NVIDIA GPU detected -- driver CUDA {maj}.{mn}")
    elif vendor == "amd":
        print("[install] AMD GPU detected (ROCm tools found)")
    elif vendor == "apple":
        print("[install] Apple Silicon detected -- Metal build")
    else:
        print("[install] no GPU acceleration detected -- CPU/Vulkan build.")
    recommended = recommend_flavor(catalog, gpu, os_key)

    flavor = args.flavor
    if flavor:
        if flavor not in catalog:
            names = sorted(catalog,
                           key=_put_default_last_key(_default_flavor_name(os_key)))
            print(f"[install] ERROR: flavor '{flavor}' not in {rel['tag']} "
                  f"for {os_key}-{arch_key}. Available: {', '.join(names)}")
            return 1
    else:
        if args.dry_run:
            print(f"[install] dry-run: would recommend '{recommended}' "
                  f"(use --flavor to force)")
            for name in sorted(catalog, key=_put_default_last_key(
                    _default_flavor_name(os_key))):
                print(f"    - {name}")
            return 0
        if not sys.stdin.isatty():
            print("[install] ERROR: no interactive console -- pass --flavor "
                  "(see --help for the list via --dry-run)")
            return 1
        flavor = choose_flavor_interactive(catalog, recommended)

    install_build(rel, flavor, catalog, args.dry_run)
    if not args.dry_run:
        if not args.no_config:
            wire_config_local(dry_run=False,
                              server_name=_binary_names()[0])
        print()
        print("[install] Next steps:")
        print("  1. point LLAMA_ARG_MODEL / LLAMA_ARG_MMPROJ in "
              "src/config_local.py at your GGUFs (models are NOT downloaded)")
        if os_key == "win":
            print("  2. start_judge.bat   (preflight + health wait)")
            print("  3. start_arena.bat   (dashboard at http://127.0.0.1:8843)")
        else:
            print("  2. python -m src.judge_server   (preflight + health wait)")
            print("  3. python -m src.arena          (dashboard :8843)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
