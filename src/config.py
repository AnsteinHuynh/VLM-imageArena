"""
VLM-imageArena configuration — ALL tunables live here.
Version: 2.9.0 (local machine overrides via config_local.py)
"""

import re
from pathlib import Path

VERSION = "2.9.0"

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
# Repo-relative by default (clonable from anywhere); wrap in Path() at use
# sites. Override here with absolute paths if you want data outside the repo.
_REPO_ROOT = Path(__file__).resolve().parents[1]
# Folder containing the candidate images (png / jpg / jpeg / webp)
IMAGE_IN_DIR = str(_REPO_ROOT / "in")
# Folder for outputs (created automatically: battles.jsonl, skips.jsonl)
# The live dashboard replaces ranking.csv/ranking.html; battles.jsonl remains
# the canonical data store.
OUT_DIR = str(_REPO_ROOT / "out")
# JSON list of judge question phrasings (editable without touching code)
PROMPTS_FILE = str(_REPO_ROOT / "src" / "prompts.json")

# ---------------------------------------------------------------------------
# Tournaments (multi-tourney support, 2026-08-06)
# ---------------------------------------------------------------------------
# CLI flag --tourney NAME selects in/{NAME}/ (images) + out/{NAME}/ (results).
# NAME "default" (or no flag) = legacy layout: in/ and out/ unchanged.
# Allowed characters: letters, digits, underscore, dash — path safety.
TOURNEY_NAME_RE = r"^[A-Za-z0-9_-]{1,64}$"


def validate_tourney_name(name):
    """True if `name` is a path-safe tourney name."""
    return bool(re.fullmatch(TOURNEY_NAME_RE, name))


def tourney_in_dir(name):
    """Image folder for a tourney: in/ for 'default', in/{name}/ otherwise."""
    base = Path(IMAGE_IN_DIR)
    return base if name == "default" else base / name


def tourney_out_dir(name):
    """Output folder for a tourney: out/ for 'default', out/{name}/ otherwise."""
    base = Path(OUT_DIR)
    return base if name == "default" else base / name


def resolve_tourney_dirs(name, in_override=None, out_override=None):
    """
    Resolve (in_dir, out_dir) for a tourney. Explicit --in/--out overrides
    beat the tourney-derived paths. Raises ValueError on invalid names.
    """
    if not validate_tourney_name(name):
        raise ValueError(
            f"invalid tourney name {name!r} — must match {TOURNEY_NAME_RE}")
    in_dir = Path(in_override) if in_override else tourney_in_dir(name)
    out_dir = Path(out_override) if out_override else tourney_out_dir(name)
    return in_dir, out_dir

SERVER_URL = "http://127.0.0.1:9932/v1/chat/completions"
SERVER_HEALTH_URL = "http://127.0.0.1:9932/health"

# The judge's identity: bind A = first-shown image, B = second-shown image.
# The task-specific context line is what makes this reusable — swap it to rank
# logos, photos, avatars, compression variants... (a past tourney judged logo
# concepts for a spooky-stories site, white = transparent background).
SYSTEM_PROMPT = (
"""
You are an image judge. You will see two images, one after the other.
The first image is A, the second image is B.
Please respond with exactly one character: \'A\' or \'B\', and nothing else.
Don't respond with 'Sure! I can do that...' or 'Based on the pictures provided...'.
Just say the letter A or B.

Context: these are two versions of the same image. Judge which one looks better in terms of overall image quality — sharpness, color fidelity, artifacts, and visual clarity. Pick the one that is the better-looking image.
"""
)

TEMPERATURE = 1.0
TOP_P = 0.95
TOP_K = 20
MIN_P = 0.0
PRESENCE_PENALTY = 1.5
REPETITION_PENALTY = 1.0
MAX_TOKENS = 512      # minimal-thinking era: room for brief CoT (server budget caps thinking) + A/B verdict
REQUEST_TIMEOUT = 120  # max seconds per query (35B + image encoding can be slow)
# Constrain the judge's reply to EXACTLY "A" or "B" via llama.cpp grammar
# sampling (per-request "grammar" field in the chat-completions body — same
# non-standard extension family as id_slot, so NO server restart needed).
# True = grammar enforced: zero malformed votes, every query counts, and the
# vote is the properly-conditioned P(A|{A,B}) (recommended mode).
# False = prompt-only: old rejection path applies (parser + skips.jsonl + pair ban).
# WARNING: never mix modes inside one tourney's battles.jsonl — a grammar
# judge votes differently than a free one (same rule as never mixing model
# quants). Flip only for A/B testing, then start a fresh tourney.
#
# 2026-08-08 ANSTEIN DECISION: OFF for now and future runs. Rationale: the
# 2026-08-08 grammar A/B test (801/802-game arenas + 150 same-seed probe +
# 360-row word-token probe) proved the judge emits exactly 'A'/'B' on 100%
# of queries even WITHOUT grammar (0 malformed, 0 skips) — the constraint is
# a no-op safety net for this model+prompt. Keeping it off removes a
# variable and lets the rejection path (skips.jsonl) do its audit job.
JUDGE_USE_GRAMMAR = False

# Judge server launch (used by src/judge_server.py via start_judge.bat)

# llama.cpp server: https://github.com/ggml-org/llama.cpp/tree/master/tools/server
# MODEL_NAME is the arbitrary string sent in the API body.
MODEL_NAME = "qwen3.8-27b"

# MACHINE-SPECIFIC — set these in src/config_local.py (gitignored), NOT here.
# Copy src/config_local.example.py to src/config_local.py and point it at
# your llama-server build + GGUF files. The judge needs a multimodal model:
# main GGUF + mmproj (vision encoder). judge_server.py preflights these
# paths and prints a friendly error if they don't resolve.
JUDGE_SERVER_EXE = r"C:\path\to\llama-server.exe"
LLAMA_ARG_MODEL = r"C:\path\to\model.gguf"
LLAMA_ARG_MMPROJ = r"C:\path\to\mmproj.gguf"


LLAMA_ARG_PORT = 9932             # must match SERVER_URL / SERVER_HEALTH_URL above
JUDGE_READY_TIMEOUT = 300         # max seconds to wait for the health endpoint after launch
ARENA_CONCURRENCY = 2   ## 4; was 12
LLAMA_ARG_N_PARALLEL = 2  ## 4 ; was 12


LLAMA_ARG_IMAGE_MAX_TOKENS = None # None = model default.
JUDGE_WEBUI = True   # 2026-08-14: serve llama-server's built-in web UI at http://127.0.0.1:9932 for PERSONAL manual testing (chat + upload images in browser). Was --no-webui. Harmless for arena runs; flip False to return to headless.

# ---------------------------------------------------------------------------
# Image preparation
# ---------------------------------------------------------------------------
MIN_TOTAL_PIXELS = 768 * 768   # images BELOW get UPSCALED (Lanczos)
MAX_TOTAL_PIXELS = 768 * 768   # images ABOVE get DOWNSCALED (Lanczos); band logic still works when MIN < MAX

# Tokens: ~1000 tokens/MP measured (linear) -> 589,824 px image ≈ 590 tokens; 2-image query ≈ 1250 tokens
# Encode format (PNG) lives in images.py as a module constant — it is a
# pipeline invariant (llama-server silently drops webp data-URIs), NOT a
# setup-dependent tunable, so it does not belong in config.

# ---------------------------------------------------------------------------
# Rating (Bradley-Terry MLE — replaces OpenSkill)
# ---------------------------------------------------------------------------
# BT-MLE fits all strengths simultaneously from the full match log.
# Order-independent: match sequence doesn't affect the final ranking.
# See bt_model.py for the math (Zermelo iteration + Fisher information CIs).

MAX_QUERIES = 200000         # hard GAME cap / insurance (games, not queries)
STOP_AT = None                   # auto-stop timestamp (Unix epoch, time.time() comparison). Loop stops gracefully when time.time() >= STOP_AT. None = run until MAX_QUERIES or manual /stop. (2026-08-14: cleared the overnight 06:00 value — it was in the past and killed every new run instantly with 0 matches.)
TOP_K_CANDIDATES = 10        # random pick among the top-K best pairs in greedy mode
MAX_MALFORMED_PER_PAIR = 5   # after this many malformed skips, ban the pair permanently

# ---------------------------------------------------------------------------
# Tournament scheduling (per-image phase tracking)
# ---------------------------------------------------------------------------
RR_CYCLES = 1                # round-robin cycles: every pair plays this many GAMES
                              # before entering the tiebreaker → greedy phase (lowered
                              # 2026-08-14: ε-greedy re-picks murky pairs anyway)
EPSILON = 0.10               # ε-greedy exploration rate: 10% random pairs, 90% info-max
                              # Active BT sampling pairs (measured β=1.4 for the real judge)
JUDGE_BETA = 1.4             # estimated judge noise (fitted from real tourney data).
                              # Used only for dashboard display / reference — BT-MLE
                              # computes its own CI widths from the Fisher information.

# ---------------------------------------------------------------------------
# Pinned-slot GAME protocol (v3, 2026-08-14 — replaces match protocol + HighLow)
# ---------------------------------------------------------------------------
# Design doc: docs/game-protocol-pinned-slots.md
# A GAME = one image pair. Each llama-server slot is PINNED to one
# (image order × question) variant; repeats on a slot hit the whole-prompt
# cache (measured 212 ms warm vs ~1124 ms cold on Qwen3.8-27B).
# QUERIES_PER_GAME = GAME_QUESTIONS × 2 orders × GAME_REPS.
# Bias cancels ACROSS slots (both orders covered equally per game) — the game
# record, never a single slot, is the unit of truth. REPS is a CAP: the
# statistical gate (θ-gap > GAME_GATE_Z·SE, checked after every query, BT fed
# at query granularity) exits early once the pair is confidently resolved.
# Scheduler = FullRR → tiebreaker → ε-greedy (HighLow retired 2026-08-14).
GAME_QUESTIONS_RR = 1        # 1 question × 2 orders = 2 variants = 2 slots, one per slot. 2Q must equal the slot count so every slot pins exactly one variant (colds are per-SLOT).
GAME_REPS_RR = 64            # reps CAP per variant, RR phase. High cap = pure gate-driven games (Anstein 2026-08-14; replaced the arbitrary 1000)
GAME_QUESTIONS_GREEDY = 1   # greedy phase: same shape as RR (1×2 = 2 variants = 2 slots — Q=2 would queue 2 variants/slot = variant-switch restore penalty mid-game)
GAME_REPS_GREEDY = 64       # greedy phase reps per variant (cap; gate usually ends games far earlier)
GAME_EARLY_EXIT = True      # master switch for the per-query statistical gate
GAME_GATE_Z = 2.5           # gate z-score (~95% CI corrected for sequential peeking after every query; 1.96 would under-cover, 4.0 is overkill — queries scale ~Z²)
GAME_SE_FLOOR = 0.15        # HOPELESS-TIE gate: when the pair's SE(gap) shrinks below this θ-unit floor and the gap STILL isn't significant, the pair is statistically indistinguishable — end the game (a tie). Calibrated 2026-08-14 from the field: SE(gap)≈0.15–0.18 at ~128 votes/pair, so the old 0.05 was unreachable (one pair ground to the 64-rep cap: 69–59). 0.15 ends near-ties at roughly 60–120 votes.
GAME_UNANIMOUS_EXIT = True  # BEST-OF-2 floor (Anstein): after every variant has fired once (2 queries at Q=1), if ALL votes went to one image (aa/bb) the game ends immediately — decisive pairs cost exactly 2 queries + 2 colds and move on. Splits (ab/ba) escalate into warm reps under the gate.
GAME_MIN_QUERIES = 25        # MINIMUM queries per game before ANY stop rule may fire (Anstein 2026-08-14: "switches too often"). Each game pays 2 colds (~2.2s GPU); ending at 2-3 queries = cold-wrapped votes (~1.1s/vote). Min 6 = 2 colds + 4 warms ≈ 0.5s/vote; the extra warm votes sharpen θ for free. Must stay <= 2 × GAME_REPS.
SEQUENTIAL_SEEDS = True     # query k uses seed k (1,2,3,...) for reproducibility (vs random seeds). Resume continues from the log's total query count (arena ≥2.3.1) — NEVER re-fire old seeds.

# ---------------------------------------------------------------------------
# Dashboard (FastAPI backend + HTML/JS frontend)
# ---------------------------------------------------------------------------
DASHBOARD_HOST = "127.0.0.1"  # localhost only by default
DASHBOARD_PORT = 8843        # arena dashboard port (frontend poll cadence lives in dashboard.html)

# ---------------------------------------------------------------------------
# Local machine overrides (gitignored) — see src/config_local.example.py
# ---------------------------------------------------------------------------
# Any ALL_CAPS variable defined in src/config_local.py replaces the value
# defined above (paths, server flags, even SYSTEM_PROMPT). This keeps
# personal paths out of the repo: config.py ships neutral placeholders and
# each machine keeps its real values in an untracked file.
try:
    from . import config_local as _local_overrides
    for _name in dir(_local_overrides):
        if _name.isupper() and not _name.startswith("_"):
            globals()[_name] = getattr(_local_overrides, _name)
    del _name, _local_overrides
except ImportError:
    pass  # no config_local.py — fine, the shipped defaults apply
