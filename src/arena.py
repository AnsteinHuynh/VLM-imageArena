"""
VLM-imageArena — orchestrator + dashboard server.
Version: 2.0.0

Replaces the old arena.py entirely. Runs the tournament loop with the new
BT-MLE scheduler and serves a live dashboard via FastAPI.

The arena is both the match runner AND the web server. The dashboard is
the primary interface — no CLI output needed beyond startup/confirms.

Phases (per-image):
    NEW → RR_ACTIVE → RR_DONE → TIEBREAK_DONE → GREEDY

Match flow:
    1. Scheduler picks a pair (RR priority → tiebreaker → ε-greedy, or pivot)
    2. Worker fires 4 queries (grouped orders, position-counterbalanced)
    3. Majority decides winner (or tie)
    4. Result logged to battles.jsonl, BT model updated, scheduler updated
    5. Dashboard serves live state via /state endpoint

Dashboard endpoints:
    GET  /              → dashboard HTML (launcher.html when nothing is loaded)
    GET  /launcher      → launcher HTML (always — "back to menu" target)
    GET  /state         → JSON: ranking, phases, match feed, pivot info
    GET  /image/<key>   → processed image (what the judge saw)
    POST /eliminate     → eliminate a player (body: {"key": "..."})
    POST /revive        → un-eliminate a player (body: {"key": "...", "hold": true})
    POST /unhold        → release a hold (body: {"key": "..."})
    POST /revive_all    → revive every eliminated player (holds cleared)
    POST /pivot         → set pivot mode (body: {"keys": [...], "mode": "...", "epsilon": 0})
    POST /pivot/clear   → clear pivot mode
    POST /scan          → scan for new images in in/{tourney}/
    POST /stop          → graceful stop after current match
    POST /resume        → start/resume the match loop
    POST /load          → load a tourney WITHOUT starting matches (view mode)
    POST /unload        → reset to launcher mode (graceful-stops if running)
    POST /create_tourney → create a new tourney folder
    POST /upload_images → upload images into a tourney folder

v2.3.0: revive/hold/revive-all (holds.json sidecar), finish reasons + live
"finished" flag, per-match vote credits (sides + score) in the feed, /launcher
+ /load + /unload endpoints, HighLow no-eligible-pair fallback to adaptive,
v3.0.0: pinned-slot GAME protocol (Q questions × 2 orders × R reps per game,
slot-pinned variants, per-query BT observations + statistical early-exit
gate). HighLow machinery removed. v2.3.x features (revive/hold/revive-all,
seed resume from log, starvation fix) retained.
"""

VERSION = "3.1.1"

import argparse
import asyncio
import json
import os
import random
import sys
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from . import config, images, judge
from .bt_model import BTModel
from .scheduler import Scheduler, PivotMode, Phase

# FastAPI for dashboard
try:
    import uvicorn
    from fastapi import FastAPI, HTTPException, Request
    from fastapi.responses import HTMLResponse, Response, JSONResponse
    import httpx
    _HAS_FASTAPI = True
except ImportError:
    _HAS_FASTAPI = False
    print("[arena] WARNING: FastAPI/uvicorn not installed. Dashboard disabled.")
    print("[arena] Install with: pip install fastapi uvicorn")


# ---------------------------------------------------------------------------
# Tournament state
# ---------------------------------------------------------------------------

class GameSpec:
    """One game: a pair battle under the pinned-slot protocol.

    variants: list of (order_tuple, q_index, question_str) — 2 per question.
    queues:   per-slot FIFO of variant refs (owner-worker-only access) — a
              slot fires its variants as contiguous rep-runs so every repeat
              after the first is a warm cache hit.
    votes:    {image_key: n} — credited votes so far (A → order[0], B → order[1]).
    subscores: {q_index: [first_votes, second_votes]} — per-question mini-verdicts.
    cancel:   set when the statistical gate resolves (or the run stops) —
              workers drop unfired queue items, game finalizes when inflight hits 0.
    """
    __slots__ = ("first", "second", "variants", "questions", "reps", "queues",
                 "fired", "total", "inflight", "cancel", "finalized", "votes",
                 "subscores", "query_log", "win_prob", "theta_before")


class Arena:
    """Holds all tournament state. Thread-safe via self.lock."""

    def __init__(self, image_dir=None, out_dir=None, tourney_name="default"):
        self.image_dir = Path(image_dir) if image_dir else None
        self.out_dir = Path(out_dir) if out_dir else None
        if self.out_dir:
            self.out_dir.mkdir(parents=True, exist_ok=True)
        self.tourney_name = tourney_name
        self.battles_path = self.out_dir / "battles.jsonl" if self.out_dir else None
        self.skips_path = self.out_dir / "skips.jsonl" if self.out_dir else None

        # Core state
        self.bt = BTModel()
        self.scheduler = Scheduler([])
        self.prepared = {}  # key → data URL (preloaded once)
        self.img_keys = []  # sorted list of image keys (paths)
        self.eliminated = set()
        self.holds = set()          # keys propped alive (user pins; persisted: holds.json)
        self._finish_reason = None  # None | 'completed' | 'stopped' | 'max' | 'exhausted'

        # Runtime
        self.lock = threading.RLock()
        self.stop_event = threading.Event()
        self.stop_after_match = False
        self._running = False
        self.skip_counts = {}  # frozenset pair → count
        self.banned = set()
        # HighLow cycle state removed (v3.0.0 — retired); game protocol state:
        self._game = None            # active GameSpec (one game at a time — all slots on one pair)
        self._n_slots = config.ARENA_CONCURRENCY  # set by run()
        self._seed_lock = threading.Lock()
        self._seed_counter = 0
        self._none_streak = 0       # consecutive pair-starved pick attempts (worker)
        self.recent_matches = deque(maxlen=50)  # dashboard feed
        self.start_time = None
        self.total_matches = 0
        self.total_ties = 0
        self.total_queries = 0

        # Rate tracking (sliding window of completion timestamps)
        self.query_times = deque()   # monotonic ts of completed queries
        self.match_times = deque()   # monotonic ts of completed matches

        # Bootstrap CI cache (recomputed on throttle — too slow for every poll)
        self._ci_cache = {}
        self._ci_cache_matches = -1
        self._ci_cache_time = 0.0

        # GPU info cache (nvidia-smi is ~200ms — poll every 5s)
        self._gpu_cache = None
        self._gpu_cache_time = 0.0

    # ------------------------------------------------------------------
    # Setup
    # ------------------------------------------------------------------

    def discover_and_load(self):
        """Discover images, prepare data URLs, replay log."""
        if not self.image_dir or not self.out_dir:
            return  # not loaded yet — launcher mode

        imgs = images.discover_images(self.image_dir)
        if not imgs:
            raise SystemExit(f"[arena] ERROR: no images found in {self.image_dir}")

        self.img_keys = [str(p) for p in imgs]
        print(f"[arena] {VERSION} | {len(self.img_keys)} images from {self.image_dir}")

        # Game protocol init (v3 — no HighLow, no top_k stopping)

        # Prepare data URLs (resize + encode — takes ~1-2s for 20 images)
        print("[arena] Preparing images...", end=" ", flush=True)
        for p in imgs:
            key = str(p)
            self.prepared[key] = images.prepare_data_url(p)
            self.scheduler.add_image(key)
            self.bt.add_player(key)
        print("done.")

        # Replay log
        self.bt.replay_log(self.battles_path)
        self.scheduler.reconstruct_from_log(self.battles_path)
        self.total_matches = self.scheduler.step

        # Count ties + queries from log for stats; seed counter MUST resume
        # after the highest seed ever used (= total queries in the log, since
        # sequential seeds are allocated 1,2,3,... under a lock). Otherwise a
        # restart re-fires seed 1..N — and identical (pair, order, seed) tuples
        # are DETERMINISTIC for the judge, so re-matched old pairs would just
        # echo their old votes (wasted queries, duplicated information).
        if self.battles_path.exists():
            with open(self.battles_path, "r", encoding="utf-8") as f:
                for line in f:
                    rec = json.loads(line.strip())
                    if rec.get("tie"):
                        self.total_ties += 1
                    self.total_queries += len(rec.get("queries") or [])
        self._seed_counter = self.total_queries

        if self.total_matches > 0:
            print(f"[arena] RESUME: replayed {self.total_matches} matches "
                  f"({self.total_ties} ties)")

        # Initial BT fit
        active = set(self.img_keys) - self.eliminated
        self.bt.fit(active)
        print(f"[arena] BT-MLE fit: {len(self.bt.players)} players, "
              f"{self.bt.decisive_matches} decisive matches")

        # Re-apply manual eliminations (persisted across restarts)
        self._load_eliminations()

    def _load_eliminations(self):
        """Re-apply manually eliminated images + holds from sidecar files."""
        elim_path = self.out_dir / "eliminated.json"
        if elim_path.exists():
            try:
                keys = json.loads(elim_path.read_text(encoding="utf-8"))
                restored = 0
                for key in keys:
                    if key in self.scheduler.states:
                        self.scheduler.states[key].eliminated = True
                        self.eliminated.add(key)
                        restored += 1
                if restored:
                    print(f"[arena] restored {restored} eliminated image(s)")
            except Exception:
                pass
        holds_path = self.out_dir / "holds.json"
        if holds_path.exists():
            try:
                for key in json.loads(holds_path.read_text(encoding="utf-8")):
                    if key in self.scheduler.states:
                        self.holds.add(key)   # revive-on-load keeps the prop
            except Exception:
                pass
        if self.eliminated:
            active = set(self.img_keys) - self.eliminated
            self.bt.fit(active)

    def _save_eliminations(self):
        """Persist the current eliminated set to a sidecar file."""
        try:
            elim_path = self.out_dir / "eliminated.json"
            elim_path.write_text(
                json.dumps(sorted(self.eliminated), ensure_ascii=False),
                encoding="utf-8")
        except Exception:
            pass

    def _save_holds(self):
        """Persist the held set to a sidecar file."""
        try:
            holds_path = self.out_dir / "holds.json"
            holds_path.write_text(
                json.dumps(sorted(self.holds), ensure_ascii=False),
                encoding="utf-8")
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Game execution — pinned-slot protocol (v3)
    # ------------------------------------------------------------------

    def _load_prompts(self):
        p = Path(config.PROMPTS_FILE)
        if not p.exists():
            return ["Which picture is better?"]
        return json.loads(p.read_text(encoding="utf-8"))

    def _next_seed(self):
        """Sequential seeds (1,2,3,...) for reproducibility, or random."""
        if config.SEQUENTIAL_SEEDS:
            with self._seed_lock:
                self._seed_counter += 1
                return self._seed_counter
        return random.randint(0, 2**31 - 1)

    def _game_shape(self, first, second):
        """(questions, reps) for a game, by scheduler phase of the two images."""
        states = self.scheduler.states
        greedy = all(
            states.get(k) is not None and states[k].phase == Phase.GREEDY
            for k in (first, second)
        )
        if greedy:
            return config.GAME_QUESTIONS_GREEDY, config.GAME_REPS_GREEDY
        return config.GAME_QUESTIONS_RR, config.GAME_REPS_RR

    def _pick_questions(self, pool, q_n):
        """Q distinct questions for one game — rotates across games so
        different games probe different phrasings (cross-game decorrelation)."""
        if q_n >= len(pool):
            return list(pool)
        return random.sample(pool, q_n)

    @staticmethod
    def _assign_variants(variants, reps, n_slots):
        """
        One variant per slot as a CONTIGUOUS rep-run. NEVER split a variant
        across slots — colds are per-SLOT (each slot pays its own vision
        encode), so duplicating a variant multiplies colds and halves the
        warm run (measured 2026-08-14: split layout hit 50% cold).
        V >= slots: some slots run 2 variants back-to-back (extra cold between
        them, still contiguous per variant).
        V <  slots: extra slots idle this game (queues empty — free symmetry:
        variants stay order/question-balanced).
        """
        queues = [[] for _ in range(n_slots)]
        idx = 0
        # first pass: one variant per slot
        for s in range(n_slots):
            if idx < len(variants):
                queues[s].extend([variants[idx]] * reps)
                idx += 1
        # leftovers (V > S): append whole rep-runs to the shortest queues
        while idx < len(variants):
            s = min(range(n_slots), key=lambda i: len(queues[i]))
            queues[s].extend([variants[idx]] * reps)
            idx += 1
        return queues

    def build_game(self, first, second):
        """Construct a GameSpec: Q questions x 2 orders x R reps over the slots."""
        q_n, reps = self._game_shape(first, second)
        questions = self._pick_questions(self._load_prompts(), q_n)
        variants = []
        for qi, question in enumerate(questions):
            variants.append(((first, second), qi, question))   # order AB
            variants.append(((second, first), qi, question))   # order BA
        g = GameSpec()
        g.first, g.second = first, second
        g.questions = questions
        g.reps = reps
        g.variants = variants
        g.queues = self._assign_variants(variants, reps, self._n_slots)
        g.fired = 0
        g.total = sum(len(q) for q in g.queues)
        g.inflight = 0
        g.cancel = False
        g.finalized = False
        g.votes = {first: 0, second: 0}
        g.subscores = {qi: [0, 0] for qi in range(len(questions))}
        g.query_log = []
        g.win_prob = self.bt.win_probability(first, second)
        g.theta_before = {
            first: self.bt.get_theta(first),
            second: self.bt.get_theta(second),
        }
        return g

    def _pair_resolved(self, a, b):
        """Early-exit gate: |theta gap| of the pair clears GAME_GATE_Z * SE
        (Fisher). Called after every query once every variant fired once."""
        active = set(self.img_keys) - self.eliminated
        gap = abs(self.bt.get_theta(a) - self.bt.get_theta(b))
        if gap <= 0:
            return False
        se = self.bt.pairwise_gap_se(active).get(frozenset((a, b)), 0.0)
        return se > 0 and gap > config.GAME_GATE_Z * se

    def _pair_hopeless(self, a, b):
        """SE-floor tie gate: the gap's SE has shrunk below GAME_SE_FLOOR and
        the pair STILL isn't resolved — statistically indistinguishable.
        End the game (tie) instead of hammering warm reps forever."""
        se = self.bt.pairwise_gap_se(
            set(self.img_keys) - self.eliminated).get(frozenset((a, b)), 0.0)
        return 0 < se < config.GAME_SE_FLOOR and not self._pair_resolved(a, b)

    def _unanimous(self, g):
        """Best-of-2 floor: one image holds ALL credited votes, other has zero."""
        return ((g.votes[g.first] > 0 and g.votes[g.second] == 0)
                or (g.votes[g.second] > 0 and g.votes[g.first] == 0))

    def _maybe_finalize(self, g):
        """Finalize the game when complete or gate-cancelled & drained.
        MUST be called under self.lock. Exactly-once via g.finalized."""
        if g.finalized or self._game is not g:
            return
        complete = g.fired >= g.total
        cancelled_done = g.cancel and g.inflight <= 0
        if not (complete or cancelled_done):
            return
        g.finalized = True
        self._game = None

        tie = g.votes[g.first] == g.votes[g.second]
        winner = None
        if not tie:
            winner = g.first if g.votes[g.first] > g.votes[g.second] else g.second
        self.scheduler.record_result(g.first, g.second, winner)
        self.total_matches += 1
        if tie:
            self.total_ties += 1
        self.match_times.append(time.monotonic())
        theta_after = {
            g.first: self.bt.get_theta(g.first),
            g.second: self.bt.get_theta(g.second),
        }

        record = {
            "game": self.total_matches,   # already incremented — no +1 (v2.3.1 fix)
            "pair": sorted([g.first, g.second]),
            "first": g.first,
            "second": g.second,
            "winner": winner,
            "tie": tie,
            "early": g.fired < g.total,
            "phase": self.scheduler.current_phase_label(),
            "protocol": {
                "q": len(g.questions),
                "r": g.reps,
                "questions": g.questions,
                "subscores": {str(qi): sc for qi, sc in g.subscores.items()},
            },
            "queries": g.query_log,
            "ts": time.time(),
        }
        with open(self.battles_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")

        # feed entry (same shape the dashboard already renders)
        sides = []
        score = [0, 0]
        for q in g.query_log:
            if q["letter"] == "A":
                credited = q["order"][0]
            elif q["letter"] == "B":
                credited = q["order"][1]
            else:
                credited = None
            if credited == g.first:
                sides.append("F"); score[0] += 1
            elif credited == g.second:
                sides.append("S"); score[1] += 1
            else:
                sides.append(None)
        self.recent_matches.appendleft({
            "first": g.first, "second": g.second,
            "winner": winner, "tie": tie,
            "first_name": Path(g.first).name,
            "second_name": Path(g.second).name,
            "ts": record["ts"],
            "phase": record["phase"],
            "game": record["game"],
            "votes": [q["letter"] for q in g.query_log],
            "sides": sides,
            "score": score,
            "win_prob": round(g.win_prob, 4),
            "theta_before_first": round(g.theta_before[g.first], 4),
            "theta_before_second": round(g.theta_before[g.second], 4),
            "theta_after_first": round(theta_after[g.first], 4),
            "theta_after_second": round(theta_after[g.second], 4),
            "games_first": self.scheduler.states[g.first].games,
            "games_second": self.scheduler.states[g.second].games,
        })
        early_tag = f" (gate exit @ {g.fired}/{g.total})" if record["early"] else ""
        wname = Path(winner).name if winner else "TIE"
        print(f"[arena] game #{record['game']}: {Path(g.first).name} vs "
              f"{Path(g.second).name} → {wname} {score[0]}–{score[1]}{early_tag}")

    # ------------------------------------------------------------------
    # Main loop (game protocol v3)
    # ------------------------------------------------------------------

    def run(self, workers=None):
        """Start the tournament game loop. Blocks until stop."""
        workers = workers or config.ARENA_CONCURRENCY
        if not judge.server_healthy():
            print(f"[arena] ERROR: judge server not reachable at {config.SERVER_HEALTH_URL}")
            print("[arena] Start it manually (start_judge.bat)")
            return
        self._n_slots = workers

        # Wall-clock start (only on first run — resume keeps the original)
        if self.start_time is None:
            self.start_time = time.time()
        self.stop_event.clear()
        self.stop_after_match = False

        print(f"[arena] GO — game protocol v3: {workers} slot-pinned workers, "
              f"ε={config.EPSILON}, gate Z={config.GAME_GATE_Z}")

        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = [pool.submit(self._worker, i) for i in range(workers)]
            try:
                for f in futures:
                    f.result()
            except KeyboardInterrupt:
                print("\n[arena] Ctrl+C — stopping after current query...")
                self.stop_event.set()
                for f in futures:
                    f.cancel()

        # Finalize any game cut short by a stop (workers joined → inflight 0).
        # Records the fired queries with early=True; remainder is dropped.
        with self.lock:
            if self._game is not None and not self._game.finalized:
                self._game.cancel = True
                self._game.inflight = 0
                self._maybe_finalize(self._game)

        elapsed = time.time() - self.start_time
        active = self.scheduler.active_list
        champ = max(active, key=lambda k: self.bt.get_theta(k)) if active else None
        print(f"\n[arena] DONE — {self.total_matches} games, {self.total_ties} ties, "
              f"{self.total_queries} queries, {elapsed:.0f}s")
        if champ:
            print(f"[arena] LEADER: {Path(champ).name} θ={self.bt.get_theta(champ):.3f}")
        print(f"[arena] Log: {self.battles_path}")

    def start_loop(self):
        """Start the game loop in a background thread (no-op if already running)."""
        with self.lock:
            if self._running:
                return False
            self._running = True
        threading.Thread(target=self._run_loop, daemon=True).start()
        return True

    def _run_loop(self):
        """Wrap run() so _running is cleared when the loop exits."""
        try:
            self.run()
        finally:
            with self.lock:
                self._running = False

    def _worker(self, worker_idx):
        """
        Slot worker: owns server slot `worker_idx`. Pulls the next queued
        variant-ref for ITS slot from the active game and fires it (warm
        repeats). Free-runs; game bookkeeping (gate, finalize) happens under
        the lock around each query.
        """
        my_queue = None  # bound to the active game's queue for this slot
        while not self.stop_event.is_set():
            fire = None
            with self.lock:
                if (self.stop_after_match
                        or self.total_matches >= config.MAX_QUERIES
                        or (config.STOP_AT and time.time() >= config.STOP_AT)):
                    if not self._finish_reason:
                        self._finish_reason = ("max" if self.total_matches >= config.MAX_QUERIES
                                               else "stopped")
                    self.stop_event.set()
                    return

                g = self._game
                if g is None or g.finalized:
                    # No active game: build the next one (or wait if starved)
                    if self.stop_after_match:
                        self.stop_event.set()
                        return
                    pair = self.scheduler.choose_pair(self.bt, self.banned)
                    if pair is None:
                        self._none_streak += 1
                        if self._none_streak > 120:
                            if not self._finish_reason:
                                self._finish_reason = "exhausted"
                            self.stop_event.set()
                            return
                        starved = True
                        g = None
                    else:
                        self._none_streak = 0
                        g = self.build_game(*pair)
                        self._game = g
                        print(f"[arena] GAME #{self.total_matches + 1}: "
                              f"{Path(g.first).name} vs {Path(g.second).name} — "
                              f"Q={len(g.questions)} R={g.reps} "
                              f"({g.total} queries)")
                        starved = False
                else:
                    starved = False

                if not starved:
                    if g.cancel:
                        # gate resolved — nothing new to fire; help finalize
                        self._maybe_finalize(g)
                    else:
                        my_queue = g.queues[worker_idx]
                        if my_queue:
                            g.inflight += 1
                            fire = my_queue.pop(0)
                        else:
                            # my slot's queue is drained; game may still be
                            # waiting on other slots — nothing to do now
                            self._maybe_finalize(g)

            if starved:
                self.stop_event.wait(0.5)
                continue
            if fire is None:
                self.stop_event.wait(0.05)  # brief nap: other slots finishing
                continue

            # ---- fire the query (outside the lock — the slow part) ----
            order, q_idx, question = fire
            seed = self._next_seed()
            try:
                letter, raw, tokens_cached = judge.query(
                    question, self.prepared[order[0]], self.prepared[order[1]],
                    seed, id_slot=worker_idx)
            except Exception as exc:
                with self.lock:
                    g.inflight -= 1
                    g.queues[worker_idx].insert(0, fire)  # retry this rep
                print(f"[arena] worker {worker_idx} query error: {exc}")
                time.sleep(1)
                continue

            # ---- record (under the lock) ----
            with self.lock:
                g.inflight -= 1
                g.fired += 1
                self.total_queries += 1
                self.query_times.append(time.monotonic())
                if letter == "A":
                    credited = order[0]
                elif letter == "B":
                    credited = order[1]
                else:
                    credited = None

                if credited is not None:
                    g.votes[credited] += 1
                    g.subscores[q_idx][0 if credited == g.first else 1] += 1
                    # BT observation at QUERY granularity + immediate refit
                    # (keeps the early-exit gate's theta current)
                    loser = g.second if credited == g.first else g.first
                    self.bt.record_match(credited, loser)
                    self.bt.fit(set(self.img_keys) - self.eliminated)

                if letter is None:
                    pk = frozenset((g.first, g.second))
                    self.skip_counts[pk] = self.skip_counts.get(pk, 0) + 1
                    if self.skip_counts.get(pk, 0) >= config.MAX_MALFORMED_PER_PAIR:
                        self.banned.add(pk)
                        g.cancel = True  # stop wasting queries on this pair

                g.query_log.append({
                    "order": list(order), "letter": letter, "raw": raw,
                    "prompt": question, "seed": seed,
                    "tokens_cached": tokens_cached, "slot": worker_idx,
                })

                # game-stop rules, in order. None may fire before
                # GAME_MIN_QUERIES queries (warm amortization floor — ending
                # games at 2-3 queries is all-cold votes). After the floor:
                # unanimous > gap gate > SE floor.
                if (config.GAME_EARLY_EXIT and not g.cancel
                        and g.fired >= max(len(g.variants),
                                           config.GAME_MIN_QUERIES)):
                    if config.GAME_UNANIMOUS_EXIT and self._unanimous(g):
                        g.cancel = True
                        print(f"[arena] unanimous: {Path(g.first).name} vs "
                              f"{Path(g.second).name} — shutout in {g.fired} "
                              f"queries, done")
                    elif self._pair_resolved(g.first, g.second):
                        g.cancel = True
                        print(f"[arena] gate: {Path(g.first).name} vs "
                              f"{Path(g.second).name} resolved — cutting reps")
                    elif self._pair_hopeless(g.first, g.second):
                        g.cancel = True
                        print(f"[arena] SE-floor: {Path(g.first).name} vs "
                              f"{Path(g.second).name} statistically tied — "
                              f"ending game at {g.fired} queries")

                self._maybe_finalize(g)

    # ------------------------------------------------------------------
    # Dashboard state
    # ------------------------------------------------------------------

    def _get_ci(self, active):
        """Cached bootstrap CI. Recomputed when matches change and >8s since last."""
        now = time.time()
        stale = (now - self._ci_cache_time > 8.0) and (self.total_matches != self._ci_cache_matches)
        if not self._ci_cache or stale:
            try:
                self._ci_cache = self.bt.bootstrap_ci(active, n_iter=1000)
            except Exception:
                self._ci_cache = self.bt.confidence_intervals(active)
            self._ci_cache_time = now
            self._ci_cache_matches = self.total_matches
        return self._ci_cache

    def _rate(self, times):
        """Sliding-window rate (events/sec) over the last 60s from a timestamp deque."""
        window = 60.0
        now = time.monotonic()
        while times and times[0] < now - window:
            times.popleft()
        if len(times) < 2:
            return 0.0
        span = times[-1] - times[0]
        if span <= 0:
            return 0.0
        return (len(times) - 1) / span

    def state_dict(self):
        """Export current state for the dashboard API."""
        with self.lock:
            if not self.is_loaded:
                return {
                    "tourney": None,
                    "loaded": False,
                    "running": False,
                    "stopping": False,
                    "available_tourneys": self.list_tourneys(),
                    "stats": {
                        "total_matches": 0,
                        "total_ties": 0,
                        "total_queries": 0,
                        "decisive_matches": 0,
                        "active_players": 0,
                        "eliminated": 0,
                        "elapsed": 0,
                    },
                }

            active = set(self.img_keys) - self.eliminated
            ci = self._get_ci(active)
            # Active players first (sorted by θ), then eliminated (greyed out at bottom)
            active_ranking = self.bt.ranking(active)
            elim_ranking = self.bt.ranking(self.eliminated) if self.eliminated else []
            ranking = active_ranking + elim_ranking

            # leader = top-ranked active image (for pairwise significance vs #1)
            leader_key = None
            leader_lo = None
            if ranking:
                leader_key, leader_theta, _ = ranking[0]
                leader_lo = leader_theta - ci.get(leader_key, 0.0)

            ranked = []
            tied_count = 0
            for key, theta, games in ranking:
                s = self.scheduler.states.get(key)
                is_elim = key in self.eliminated
                is_held = key in self.holds and not is_elim
                rr_done, rr_total = self._rr_progress(s, active)
                c = ci.get(key, 0.0) if not is_elim else 0.0
                # "tied with #1": this image's CI overlaps the leader's CI, so
                # it is NOT statistically distinguishable from the top image.
                tied = True
                if not is_elim and leader_lo is not None and key != leader_key:
                    tied = (theta + c) >= leader_lo
                    if tied:
                        tied_count += 1
                ranked.append({
                    "key": key,
                    "name": Path(key).name,
                    "theta": round(theta, 4),
                    "ci": round(c, 4),
                    "ci_lower": round(theta - c, 4),
                    "ci_upper": round(theta + c, 4),
                    "games": games,
                    "wins": s.wins if s else 0,
                    "losses": s.losses if s else 0,
                    "phase": s.phase.value if s else "UNKNOWN",
                    "eliminated": is_elim,
                    "held": is_held,
                    "pivot_selected": key in self.scheduler.pivot_targets,
                    "rr_done": rr_done if not is_elim else 0,
                    "rr_total": rr_total if not is_elim else 0,
                    "tied_with_leader": tied,
                })

            # Adjacent-rank gap significance (within the active ranking, sorted
            # by θ desc). gap_sig = the θ gap to the rank above exceeds
            # 1.645 × SE(gap) from the Fisher covariance.
            gap_se = self.bt.pairwise_gap_se(active)
            active_ranked = [e for e in ranked if not e["eliminated"]]
            for idx, e in enumerate(active_ranked):
                if idx == 0:
                    e["gap_se"] = 0.0
                    e["gap_sig"] = False
                else:
                    above = active_ranked[idx - 1]
                    se = gap_se.get(frozenset((above["key"], e["key"])), 0.0)
                    gap = above["theta"] - e["theta"]
                    e["gap_se"] = round(se, 4)
                    e["gap_sig"] = bool(se > 0 and gap > 1.645 * se)

            # Diagnostics (mirrors the scheduler-playground metrics)
            active_games = [self.scheduler.states[k].games for k in active
                            if k in self.scheduler.states]
            if len(active_games) >= 2 and min(active_games) > 0:
                games_ratio = max(active_games) / min(active_games)
            else:
                games_ratio = 1.0
            starvation = sum(
                1 for k in active
                if self.scheduler.states.get(k)
                and self.total_matches - self.scheduler.states[k].last_played_step > 50
                and self.total_matches > 50
            )
            thr = 0.2 * config.JUDGE_BETA
            wasted = 0
            seen_pairs = set()
            for key, s in self.scheduler.states.items():
                for opp, cnt in s.pair_counts.items():
                    pair = frozenset((key, opp))
                    if pair in seen_pairs:
                        continue
                    seen_pairs.add(pair)
                    if ci.get(key, 99) < thr and ci.get(opp, 99) < thr:
                        wasted += cnt

            return {
                "tourney": self.tourney_name,
                "loaded": True,
                "running": self._running,
                "stopping": self.stop_after_match and self._running,
                "finished": (self.total_matches > 0 and not self._running
                             and not self.stop_after_match
                             and self._finish_reason in ("max", "exhausted")),
                "finish_reason": self._finish_reason,
                "available_tourneys": self.list_tourneys(),
                "phase_label": self.scheduler.current_phase_label(),
                "ranking": ranked,
                "significance": {
                    "leader": Path(leader_key).name if leader_key else None,
                    "tied_count": tied_count,
                    "active": len(active),
                },
                "recent_matches": list(self.recent_matches),
                "graph": self._graph_data(),
                "stats": {
                    "total_matches": self.total_matches,
                    "total_ties": self.total_ties,
                    "total_queries": self.total_queries,
                    "decisive_matches": self.bt.decisive_matches,
                    "active_players": len(active),
                    "eliminated": len(self.eliminated),
                    "held": len(self.holds & active),
                    "elapsed": time.time() - self.start_time if self.start_time else 0,
                    "queries_per_sec": round(self._rate(self.query_times), 2),
                    "games_per_sec": round(self._rate(self.match_times), 2),
                    "games_ratio": round(games_ratio, 2),
                    "starvation": starvation,
                    "wasted_matches": wasted,
                    "pixel_budget": config.MAX_TOTAL_PIXELS,
                },
                "pivot": {
                    "mode": self.scheduler.pivot_mode.value,
                    "targets": list(self.scheduler.pivot_targets),
                    "epsilon": self.scheduler.pivot_epsilon,
                },
                "gpu": self._gpu_info(),
                "config": {
                    "epsilon": config.EPSILON,
                    "rr_cycles": config.RR_CYCLES,
                    "game_q_rr": config.GAME_QUESTIONS_RR,
                    "game_reps_rr": config.GAME_REPS_RR,
                    "game_q_greedy": config.GAME_QUESTIONS_GREEDY,
                    "game_reps_greedy": config.GAME_REPS_GREEDY,
                    "game_gate_z": config.GAME_GATE_Z,
                    "beta": config.JUDGE_BETA,
                },
            }

    def _rr_progress(self, s, active):
        """Per-image round-robin coverage: (done, total) matches toward 2× coverage."""
        if s is None:
            return 0, 0
        total = config.RR_CYCLES * max(0, len(active) - 1)
        done = 0
        for opp in active:
            if opp == s.key:
                continue
            done += min(s.pair_counts.get(opp, 0), config.RR_CYCLES)
        return done, total

    def _graph_data(self):
        """Export node + edge data for the circular graph visualization."""
        nodes = []
        for key in self.img_keys:
            s = self.scheduler.states.get(key)
            nodes.append({
                "key": key,
                "name": Path(key).name,
                "theta": round(self.bt.get_theta(key), 4),
                "games": s.games if s else 0,
                "eliminated": key in self.eliminated,
                "phase": s.phase.value if s else "UNKNOWN",
            })
        edges = []
        seen = set()
        for key, s in self.scheduler.states.items():
            for opp, count in s.pair_counts.items():
                if count <= 0:
                    continue
                pair = frozenset((key, opp))
                if pair not in seen:
                    seen.add(pair)
                    edges.append({"a": key, "b": opp, "count": count})
        return {"nodes": nodes, "edges": edges}

    # ------------------------------------------------------------------
    # Dashboard actions
    # ------------------------------------------------------------------

    def do_eliminate(self, key):
        """Eliminate a player."""
        with self.lock:
            if key not in self.scheduler.states or self.scheduler.states[key].eliminated:
                return False
            self.scheduler.eliminate(key)
            self.eliminated.add(key)
            # Refit BT on remaining players
            active = set(self.img_keys) - self.eliminated
            self.bt.fit(active)
            self._save_eliminations()
            self._save_holds()
            return True

    def do_revive(self, key, hold=True):
        """
        Bring an eliminated player back. By default the player is HELD
        (pinned alive — a marker pin; no auto-elimination exists in v3) so it can't be
        instantly re-benched; release the hold via do_unhold and the player
        stays alive only as long as it earns it.
        """
        with self.lock:
            if key not in self.scheduler.states:
                return False
            if key in self.eliminated:
                self.scheduler.revive(key)
                self.eliminated.discard(key)
            if hold:
                self.scheduler.hold(key)
                self.holds.add(key)
            else:
                self.scheduler.unhold(key)
                self.holds.discard(key)
            active = set(self.img_keys) - self.eliminated
            self.bt.fit(active)
            self._save_eliminations()
            self._save_holds()
            return True

    def do_unhold(self, key):
        """Release a hold. The player stays active but becomes elimination-eligible again."""
        with self.lock:
            if key not in self.holds:
                return False
            self.scheduler.unhold(key)
            self.holds.discard(key)
            self._save_holds()
            return True

    def do_revive_all(self):
        """
        Revive EVERY eliminated player (holds cleared — fresh full-field battle).
        Returns the number revived. Loop must be (re)started separately.
        """
        with self.lock:
            n = len(self.eliminated)
            for key in list(self.eliminated):
                self.scheduler.revive(key)
            self.eliminated.clear()
            self.holds.clear()
            self.scheduler.held_keys.clear()
            self._finish_reason = None
            if n:
                active = set(self.img_keys) - self.eliminated
                self.bt.fit(active)
            self._save_eliminations()
            self._save_holds()
            return n

    def _gpu_info(self):
        """Query nvidia-smi for VRAM usage + GPU temp (cached ~5s)."""
        now = time.time()
        if now - self._gpu_cache_time < 5.0 and self._gpu_cache is not None:
            return self._gpu_cache
        self._gpu_cache_time = now
        try:
            import subprocess
            out = subprocess.run(
                ["nvidia-smi", "--query-gpu=memory.used,memory.total,temperature.gpu",
                 "--format=csv,noheader,nounits"],
                capture_output=True, text=True, timeout=3,
            )
            if out.returncode == 0 and out.stdout.strip():
                used, total, temp = out.stdout.strip().splitlines()[0].split(",")
                self._gpu_cache = {
                    "vram_used": int(used.strip()),
                    "vram_total": int(total.strip()),
                    "temperature": int(temp.strip()),
                }
            else:
                self._gpu_cache = None
        except Exception:
            self._gpu_cache = None
        return self._gpu_cache

    def do_pivot(self, keys, mode_str, epsilon=0.0):
        """Set pivot mode."""
        with self.lock:
            mode = PivotMode(mode_str) if mode_str != "NONE" else PivotMode.NONE
            if mode == PivotMode.NONE:
                self.scheduler.clear_pivot()
            else:
                self.scheduler.set_pivot(set(keys), mode, epsilon)
            return True

    def do_scan(self):
        """Scan for new images and add them."""
        with self.lock:
            imgs = images.discover_images(self.image_dir)
            added = []
            for p in imgs:
                key = str(p)
                if key not in self.scheduler.states:
                    self.prepared[key] = images.prepare_data_url(p)
                    self.scheduler.add_image(key)
                    self.bt.add_player(key)
                    self.img_keys.append(key)
                    added.append(Path(key).name)
            return added

    def do_stop(self):
        """Signal graceful stop after the current match."""
        with self.lock:
            self.stop_after_match = True

    def do_resume(self):
        """Resume the match loop after a stop."""
        return self.start_loop()

    def load_tourney(self, name):
        """Load a tourney by name (from the launcher). Resets all state."""
        in_dir, out_dir = config.resolve_tourney_dirs(name)
        self.image_dir = in_dir
        self.out_dir = out_dir
        self.out_dir.mkdir(parents=True, exist_ok=True)
        self.tourney_name = name
        self.battles_path = self.out_dir / "battles.jsonl"
        self.skips_path = self.out_dir / "skips.jsonl"

        # Reset state
        self.bt = BTModel()
        self.scheduler = Scheduler([])
        self.prepared = {}
        self.img_keys = []
        self.eliminated = set()
        self.holds = set()
        self._finish_reason = None
        self.stop_event.clear()
        self.stop_after_match = False
        self.skip_counts.clear()
        self.banned.clear()
        self.recent_matches.clear()
        self.start_time = None
        self.total_matches = 0
        self.total_ties = 0
        self.total_queries = 0
        self._seed_counter = 0
        self._none_streak = 0

        self.discover_and_load()

    @property
    def is_loaded(self):
        """True if a tourney has been loaded."""
        return self.image_dir is not None and len(self.img_keys) > 0

    def unload(self):
        """Reset to launcher mode (no tourney loaded). Caller must ensure the loop is stopped."""
        with self.lock:
            self.image_dir = None
            self.out_dir = None
            self.tourney_name = "default"
            self.battles_path = None
            self.skips_path = None
            self.bt = BTModel()
            self.scheduler = Scheduler([])
            self.prepared = {}
            self.img_keys = []
            self.eliminated = set()
            self.holds = set()
            self.skip_counts.clear()
            self.banned.clear()
            self.recent_matches.clear()
            self.start_time = None
            self.total_matches = 0
            self.total_ties = 0
            self.total_queries = 0
            self._seed_counter = 0
            self._none_streak = 0
            self._finish_reason = None

    @staticmethod
    def list_tourneys():
        """Scan in/ for available tournament folders + image counts."""
        base = Path(config.IMAGE_IN_DIR)
        tourneys = []
        # Default tourney (images directly in in/)
        if base.is_dir():
            default_imgs = [p for p in base.iterdir()
                           if p.is_file() and p.suffix.lower() in images.SUPPORTED_EXT]
            if default_imgs:
                tourneys.append({
                    "name": "default",
                    "image_count": len(default_imgs),
                    "has_log": (Path(config.OUT_DIR) / "battles.jsonl").exists(),
                })
            # Named tourneys (subfolders)
            for d in sorted(base.iterdir()):
                if d.is_dir() and not d.name.startswith("."):
                    imgs = [p for p in d.iterdir()
                           if p.is_file() and p.suffix.lower() in images.SUPPORTED_EXT]
                    if imgs:
                        log_path = Path(config.OUT_DIR) / d.name / "battles.jsonl"
                        tourneys.append({
                            "name": d.name,
                            "image_count": len(imgs),
                            "has_log": log_path.exists(),
                        })
        return tourneys


# ---------------------------------------------------------------------------
# Dashboard server
# ---------------------------------------------------------------------------

def create_dashboard_app(arena):
    """Create a FastAPI app serving the dashboard for an Arena instance."""
    if not _HAS_FASTAPI:
        return None

    app = FastAPI(title="VLM-imageArena Dashboard")

    # Serve the dashboard HTML (handles both launcher and tournament modes via JS)
    dashboard_html_path = Path(__file__).parent / "static" / "dashboard.html"
    launcher_html_path = Path(__file__).parent / "static" / "launcher.html"

    @app.get("/", response_class=HTMLResponse)
    async def serve_dashboard():
        if not arena.is_loaded and launcher_html_path.exists():
            return HTMLResponse(launcher_html_path.read_text(encoding="utf-8"))
        if dashboard_html_path.exists():
            return HTMLResponse(dashboard_html_path.read_text(encoding="utf-8"))
        return HTMLResponse("<h1>dashboard.html not found</h1>", status_code=404)

    @app.get("/launcher", response_class=HTMLResponse)
    async def serve_launcher():
        """Launcher page — always served (dashboard '⌂ menu' navigates here)."""
        if launcher_html_path.exists():
            return HTMLResponse(launcher_html_path.read_text(encoding="utf-8"))
        return HTMLResponse("<h1>launcher.html not found</h1>", status_code=404)

    @app.get("/state")
    async def get_state():
        return JSONResponse(arena.state_dict())

    @app.post("/start")
    async def start_tourney(body: dict):
        """Load a tourney and start the match loop (idempotent when already loaded)."""
        name = body.get("name")
        if not name:
            raise HTTPException(400, "missing 'name'")
        if arena.is_loaded:
            # Already loaded: just make sure the loop is running (resume from launcher)
            if not arena._running:
                arena.start_loop()
            return {"ok": True, "tourney": arena.tourney_name}
        try:
            arena.load_tourney(name)
        except SystemExit as e:
            raise HTTPException(400, str(e))
        except Exception as e:
            raise HTTPException(500, str(e))
        # Start the match loop in a background thread
        arena.start_loop()
        return {"ok": True, "tourney": name}

    @app.post("/load")
    async def load_tourney_view(body: dict):
        """Load a tourney WITHOUT starting matches (view/inspect mode)."""
        name = body.get("name")
        if not name:
            raise HTTPException(400, "missing 'name'")
        if arena.is_loaded:
            raise HTTPException(409, "a tournament is already loaded (unload first)")
        try:
            arena.load_tourney(name)
        except SystemExit as e:
            raise HTTPException(400, str(e))
        except Exception as e:
            raise HTTPException(500, str(e))
        return {"ok": True, "tourney": name}

    @app.post("/unload")
    async def unload_arena():
        """Graceful-stop (if running) and reset to launcher mode."""
        if arena._running:
            arena.do_stop()
            deadline = time.time() + 20
            while arena._running and time.time() < deadline:
                await asyncio.sleep(0.5)
            if arena._running:
                raise HTTPException(409, "match loop still finishing — retry in a moment")
        arena.unload()
        return {"ok": True}

    @app.get("/image/{key:path}")
    async def get_image(key: str):
        """Serve the processed image (what the judge saw)."""
        # The prepared data URL is base64-encoded. Extract and return it.
        data_url = arena.prepared.get(key)
        if not data_url:
            raise HTTPException(404, "image not found")
        # data:image/png;base64,XXXX → strip prefix
        if "," in data_url:
            header, b64 = data_url.split(",", 1)
            import base64
            img_bytes = base64.b64decode(b64)
            return Response(content=img_bytes, media_type="image/png")
        raise HTTPException(500, "invalid data URL")

    @app.post("/eliminate")
    async def eliminate(body: dict):
        key = body.get("key")
        if not key:
            raise HTTPException(400, "missing 'key'")
        if arena.do_eliminate(key):
            return {"ok": True, "key": key}
        raise HTTPException(404, "player not found or already eliminated")

    @app.post("/revive")
    async def revive(body: dict):
        """Revive an eliminated player (hold=true pins it alive, default true)."""
        key = body.get("key")
        hold = body.get("hold", True)
        if not key:
            raise HTTPException(400, "missing 'key'")
        if arena.do_revive(key, hold=bool(hold)):
            return {"ok": True, "key": key, "held": bool(hold)}
        raise HTTPException(404, "player not found")

    @app.post("/unhold")
    async def unhold(body: dict):
        key = body.get("key")
        if not key:
            raise HTTPException(400, "missing 'key'")
        if arena.do_unhold(key):
            return {"ok": True, "key": key}
        raise HTTPException(404, "player is not held")

    @app.post("/revive_all")
    async def revive_all():
        """Revive every eliminated player (holds cleared) — pair with /resume."""
        n = arena.do_revive_all()
        if not arena._running:
            arena.start_loop()
        return {"ok": True, "revived": n}

    @app.post("/pivot")
    async def set_pivot(body: dict):
        keys = body.get("keys", [])
        mode = body.get("mode", "PIVOT_RR")
        epsilon = body.get("epsilon", 0.0)
        arena.do_pivot(keys, mode, epsilon)
        return {"ok": True}

    @app.post("/pivot/clear")
    async def clear_pivot():
        arena.do_pivot([], "NONE", 0.0)
        return {"ok": True}

    @app.post("/scan")
    async def scan_new():
        added = arena.do_scan()
        return {"ok": True, "added": added}

    @app.post("/stop")
    async def stop_arena():
        arena.do_stop()
        return {"ok": True}

    @app.post("/resume")
    async def resume_arena():
        ok = arena.do_resume()
        return {"ok": ok}

    @app.post("/create_tourney")
    async def create_tourney(body: dict):
        """Create a new tournament folder."""
        name = body.get("name", "").strip()
        if not name or not config.validate_tourney_name(name):
            raise HTTPException(400, "invalid tourney name (letters, digits, underscore, dash only)")
        tourney_in = Path(config.IMAGE_IN_DIR) / name
        if tourney_in.exists():
            raise HTTPException(409, f"tournament '{name}' already exists")
        tourney_in.mkdir(parents=True, exist_ok=True)
        return {"ok": True, "name": name}

    @app.post("/upload_images")
    async def upload_images(request: Request):
        """Upload image files to a tourney folder via multipart form data."""
        form = await request.form()
        name = form.get("tourney", "")
        if not name:
            raise HTTPException(400, "missing 'tourney' field")
        tourney_in = Path(config.IMAGE_IN_DIR) / name
        if not tourney_in.is_dir():
            raise HTTPException(404, f"tournament folder '{name}' not found")
        saved = []
        # Collect all files (form key "files")
        files = form.getlist("files")
        for f in files:
            if hasattr(f, 'filename') and f.filename:
                fname = Path(f.filename).name  # strip any path components
                ext = Path(fname).suffix.lower()
                if ext not in images.SUPPORTED_EXT:
                    continue
                dest = tourney_in / fname
                content = await f.read()
                dest.write_bytes(content)
                saved.append(fname)
        return {"ok": True, "name": name, "uploaded": saved}

    return app


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="VLM-imageArena — BT-MLE tournament")
    parser.add_argument("--tourney", default=None,
                        help="skip launcher and start this tourney directly")
    parser.add_argument("--no-dashboard", action="store_true",
                        help="disable the dashboard server")
    parser.add_argument("--dry-run", action="store_true",
                        help="discover images + show scheduler state, no matches")
    args = parser.parse_args()

    if not _HAS_FASTAPI:
        print("[arena] ERROR: FastAPI/uvicorn not installed. Run: pip install fastapi uvicorn")
        return

    # If --tourney given, load it directly (CLI mode)
    arena = Arena()
    if args.tourney:
        arena.load_tourney(args.tourney)
        if args.dry_run:
            print(f"\n[arena] DRY RUN — scheduler state:")
            print(f"  Phase: {arena.scheduler.current_phase_label()}")
            for key in arena.scheduler.active_list:
                s = arena.scheduler.states[key]
                print(f"  {Path(key).name:44s} phase={s.phase.value:14s} "
                      f"games={s.games:4d} θ={arena.bt.get_theta(key):+.3f}")
            return

    # Start dashboard server (blocks)
    app = create_dashboard_app(arena)

    # Auto-open browser
    import webbrowser
    url = f"http://{config.DASHBOARD_HOST}:{config.DASHBOARD_PORT}"
    threading.Timer(1.0, lambda: webbrowser.open(url)).start()

    print(f"[arena] VLM-imageArena v{VERSION}")
    print(f"[arena] Dashboard: {url}")
    if not arena.is_loaded:
        print("[arena] No tourney loaded — pick one from the launcher.")

    # If tourney pre-loaded via CLI, start match loop in background
    if arena.is_loaded:
        arena.start_loop()

    # Run the web server (blocks forever)
    uvicorn.run(app, host=config.DASHBOARD_HOST,
                port=config.DASHBOARD_PORT, log_level="warning")


if __name__ == "__main__":
    main()
