"""
VLM-imageArena — image discovery + total-pixel normalization (Lanczos).
Version: 1.1.0

Images are normalized to a TOTAL-PIXEL budget (e.g. 768*768 = 589,824 px),
not megapixels. Equal pixels = equal image tokens = no token-count bias.

Cache (v1.1.0): prepare_data_url() returns a ready-to-send data URL. The
arena/championship preload dict holds those strings in RAM — one encode per
image per run, base64 never rebuilt per query. A best-effort disk mirror
lives in <source parent>/.cache (only for images inside IMAGE_IN_DIR) so a
restart skips re-encoding and the exact judge-visible pixels are
inspectable on disk. The mirror is content-addressed: any change to the
source (mtime/size) or prep settings yields a new key, so stale files are
orphaned, never misread.

v1.2.0: ENCODE_FORMAT moved here from config (pipeline invariant, not a
setup tunable). PNG is mandatory — llama-server silently drops webp
config — and lossless, so JPEG-quality knobs are gone too.
"""

VERSION = "1.2.0"

import base64
import hashlib
import io
import os
import tempfile
from pathlib import Path

from PIL import Image

from . import config

SUPPORTED_EXT = {".png", ".jpg", ".jpeg", ".webp"}
CACHE_SUBDIR = ".cache"  # disk mirror: <source parent>/.cache
ENCODE_FORMAT = "PNG"    # pipeline invariant (NOT config): llama-server silently drops webp data-URIs, and lossless PNG avoids JPEG-artifact judging bias


def discover_images(folder):
    """Return sorted list of candidate image paths in `folder` ([] if missing)."""
    folder = Path(folder)
    if not folder.is_dir():
        return []
    return sorted(
        p for p in folder.iterdir()
        if p.is_file() and p.suffix.lower() in SUPPORTED_EXT
    )


def total_pixels(img):
    """Pixel count of a PIL image (w*h)."""
    return img.width * img.height


def prepare_image(path):
    """
    Load an image and normalize it into the total-pixel band [MIN, MAX].
    Above MAX -> downscale (Lanczos). Below MIN -> upscale (Lanczos).
    Returns encoded bytes ready for base64 transport.
    """
    if config.MIN_TOTAL_PIXELS > config.MAX_TOTAL_PIXELS:
        raise ValueError(
            f"MIN_TOTAL_PIXELS ({config.MIN_TOTAL_PIXELS}) > MAX_TOTAL_PIXELS "
            f"({config.MAX_TOTAL_PIXELS}) — band is inverted"
        )
    with Image.open(path) as im:
        im = im.convert("RGB")
        px = total_pixels(im)
        if px > config.MAX_TOTAL_PIXELS:
            factor = (config.MAX_TOTAL_PIXELS / px) ** 0.5
            im = im.resize(_scaled_size(im, factor), Image.LANCZOS)
        elif px < config.MIN_TOTAL_PIXELS:
            factor = (config.MIN_TOTAL_PIXELS / px) ** 0.5
            im = im.resize(_scaled_size(im, factor), Image.LANCZOS)
        buf = io.BytesIO()
        im.save(buf, format=ENCODE_FORMAT)  # PNG ignores quality knobs (lossless)
        return buf.getvalue()


def _cache_key(path):
    """Content-addressed key for the disk mirror.

    Inputs: resolved source path + mtime_ns + size + pixel band + encode
    format. Any change → new key → old mirror file orphaned (harmless).
    """
    st = path.stat()
    h = hashlib.sha256()
    h.update(str(path.resolve()).encode("utf-8", "surrogateescape"))
    h.update(f"|{st.st_mtime_ns}|{st.st_size}".encode("utf-8"))
    h.update(
        f"|{config.MIN_TOTAL_PIXELS}x{config.MAX_TOTAL_PIXELS}"
        f"|{ENCODE_FORMAT}".encode("utf-8")
    )
    return h.hexdigest()


def _cache_dir(path):
    """Mirror folder for a source path, or None to skip mirroring.

    Mirrors only live next to arena inputs (inside IMAGE_IN_DIR) — sources
    from elsewhere (e.g. CHAMPIONSHIP_EXTRA_IMAGES pointing at arbitrary
    folders) never litter their own directories; they still get RAM-cached
    via the caller's preload dict, just not mirrored to disk.
    """
    parent = Path(path).resolve().parent
    base = Path(config.IMAGE_IN_DIR).resolve()
    try:
        parent.relative_to(base)
    except ValueError:
        return None
    return parent / CACHE_SUBDIR


def prepare_data_url(path):
    """Prepare `path` and return a ready-to-send data URL.

    RAM caching is the caller's preload dict (one call per image per run —
    arena.py and championship.py both build {path: prepare_data_url(path)}).
    Disk mirror <source parent>/.cache/<key>.<ext> is consulted on miss and
    written atomically (tmp + rename) on miss; any OSError falls back to
    in-memory prep — the mirror is best-effort, never a failure point.
    """
    path = Path(path)
    key = _cache_key(path)
    ext = ENCODE_FORMAT.lower()
    cache_dir = _cache_dir(path)
    blob = None
    mirror = None
    if cache_dir is not None:
        mirror = cache_dir / f"{key}.{ext}"
        try:
            if mirror.is_file():
                blob = mirror.read_bytes()
            else:
                cache_dir.mkdir(parents=True, exist_ok=True)
        except OSError:
            mirror = None  # mirror unusable — fall through to in-memory prep
    if blob is None:
        blob = prepare_image(path)  # PIL decode + resize + encode
        if mirror is not None:
            try:
                fd, tmp = tempfile.mkstemp(dir=cache_dir, suffix=".tmp")
                with os.fdopen(fd, "wb") as fh:
                    fh.write(blob)
                os.replace(tmp, mirror)  # atomic: crash never leaves a partial file
            except OSError:
                pass  # best-effort — RAM cache still works
    b64 = base64.b64encode(blob).decode("ascii")
    return f"data:image/{ext};base64,{b64}"


def _scaled_size(im, factor):
    """Aspect-preserving target size for a resize factor."""
    return (
        max(1, round(im.width * factor)),
        max(1, round(im.height * factor)),
    )
