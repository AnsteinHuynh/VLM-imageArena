"""
VLM-imageArena — per-image phase scheduler with ε-greedy pair selection.
Version: 1.1.0

Each image has its own lifecycle:
    NEW → RR_ACTIVE → RR_DONE → GREEDY

(TIEBREAK_DONE phase removed 2026-08-14, user decision — all-tie pairs are
handled by the game protocol's SE-floor rule instead of a dedicated phase.)

Scheduler priority (game protocol v3, HighLow retired 2026-08-14):
    1. RR needs (images that haven't played everyone ≥RR_CYCLES games)
    2. ε-greedy info-max (90% best-info pair, 10% random)

v1.4.0: tiebreaker phase removed (choose_pair: RR → greedy directly;
_update_phases promotes RR_DONE → GREEDY when the whole field is done with RR).

v1.3.0: HighLow mode REMOVED (was a bandaid for slow cold queries; the
pinned-slot game protocol made queries cheap — see
docs/game-protocol-pinned-slots.md). held_keys kept: holds are user pins
(manual eliminate/revive flow), no automatic elimination exists anymore.

v1.2.0: held_keys — players propped alive by the user.

Overlay modes:
    PIVOT_RR:     selected images are round-robined against all others
    PIVOT_GREEDY: selected images are paired greedily (ε overridable to 0)
"""

VERSION = "1.4.0"

import random
from collections import defaultdict
from enum import Enum

from . import config


class Phase(Enum):
    """Per-image lifecycle phase."""
    NEW = "NEW"                    # just added, zero matches
    RR_ACTIVE = "RR_ACTIVE"        # playing round-robin
    RR_DONE = "RR_DONE"            # ≥RR_CYCLES games vs every active player
    GREEDY = "GREEDY"              # fully integrated, ε-greedy eligible
    # TIEBREAK_DONE removed in v1.4.0 (kept below ONLY to keep old exported
    # state files loadable; never assigned anymore)
    TIEBREAK_DONE = "TIEBREAK_DONE"


class PivotMode(Enum):
    NONE = "NONE"
    RR = "PIVOT_RR"
    GREEDY = "PIVOT_GREEDY"


class ImageState:
    """Per-image tracking for the scheduler."""

    def __init__(self, key):
        self.key = key
        self.phase = Phase.NEW
        self.eliminated = False
        self.elim_step = None

        # pair_counts[opponent_key] = total matches played vs opponent
        self.pair_counts = defaultdict(int)
        # decisive_counts[opponent_key] = decisive (non-tie) matches vs opponent
        self.decisive_counts = defaultdict(int)
        # total games (decisive only — ties carry no BT info)
        self.games = 0
        self.wins = 0
        self.losses = 0
        self.last_played_step = -1

    def rr_complete(self, active_keys):
        """True if this image has played ≥ RR_CYCLES matches vs every active opponent."""
        for opp in active_keys:
            if opp == self.key:
                continue
            if self.pair_counts[opp] < config.RR_CYCLES:
                return False
        return True

    def __repr__(self):
        return f"ImageState({self.key[-20:]}, {self.phase.name}, games={self.games})"


class Scheduler:
    """
    Picks the next pair to play based on per-image phase tracking.

    State:
        states: {image_key: ImageState}
        step:   total matches scheduled (including ties)
        active_keys: set of non-eliminated image keys (derived from states)
        pivot_targets: set of selected image keys for pivot mode
        pivot_mode: PivotMode enum
        pivot_epsilon: epsilon override during greedy pivot (0 = pure greedy)
        epsilon: normal-phase epsilon (config.EPSILON)
    """

    def __init__(self, image_keys, rng=None):
        self.states = {k: ImageState(k) for k in image_keys}
        self.step = 0
        self.pivot_targets = set()
        self.pivot_mode = PivotMode.NONE
        self.pivot_epsilon = 0.0
        self.held_keys = set()   # user pins (no auto-elimination since v1.3.0; kept for the revive flow)
        self.epsilon = config.EPSILON
        self._rng = rng or random
        self._rr_cursor = defaultdict(int)  # image_key → next opponent index for RR

    @property
    def active_keys(self):
        """Currently active (non-eliminated) image keys."""
        return {k for k, s in self.states.items() if not s.eliminated}

    @property
    def active_list(self):
        """Sorted list of active keys (stable iteration order)."""
        return sorted(self.active_keys)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def add_image(self, key):
        """Add a new image mid-tournament (starts as NEW → gets welcome tour)."""
        if key not in self.states:
            self.states[key] = ImageState(key)

    def eliminate(self, key):
        """Mark an image as eliminated (also drops any hold — eliminated trumps held)."""
        if key in self.states:
            self.states[key].eliminated = True
            self.states[key].elim_step = self.step
            self.pivot_targets.discard(key)
            self.held_keys.discard(key)

    def revive(self, key):
        """Un-eliminate an image (arena owns whether it gets a hold)."""
        if key in self.states:
            self.states[key].eliminated = False

    def hold(self, key):
        """Prop a player alive: protected from HighLow elimination (worst side)."""
        if key in self.states and not self.states[key].eliminated:
            self.held_keys.add(key)

    def unhold(self, key):
        """Release the prop — player becomes elimination-eligible again."""
        self.held_keys.discard(key)

    def record_result(self, a, b, winner):
        """
        Record a match result.
        a, b: the two image keys (order = display order).
        winner: key of winner, or None for tie.
        """
        self.step += 1
        sa, sb = self.states.get(a), self.states.get(b)
        if sa is None or sb is None:
            return
        sa.pair_counts[b] += 1
        sb.pair_counts[a] += 1
        sa.last_played_step = self.step
        sb.last_played_step = self.step
        if winner is not None:
            sa.decisive_counts[b] += 1
            sb.decisive_counts[a] += 1
            sa.games += 1
            sb.games += 1
            if winner == a:
                sa.wins += 1
                sb.losses += 1
            else:
                sb.wins += 1
                sa.losses += 1
        self._update_phases()

    def enter_highlow(self):
        """Enter HighLow elimination mode (leader vs worst)."""
        self.highlow_active = True

    def exit_highlow(self):
        """Exit HighLow elimination mode."""
        self.highlow_active = False

    def all_greedy(self):
        """True if every active image has completed RR + tiebreaker (all GREEDY)."""
        active = self.active_list
        if not active:
            return False
        return all(self.states[k].phase == Phase.GREEDY for k in active)

    def set_pivot(self, targets, mode, epsilon=0.0):
        """
        Set pivot mode.
        targets: set of image keys to focus on.
        mode: PivotMode.RR, PivotMode.GREEDY, or PivotMode.NONE.
        epsilon: override for greedy pivot (0 = pure greedy on targets).
        """
        self.pivot_targets = {t for t in targets if t in self.states and not self.states[t].eliminated}
        self.pivot_mode = mode
        self.pivot_epsilon = epsilon

    def clear_pivot(self):
        """Exit pivot mode."""
        self.pivot_mode = PivotMode.NONE
        self.pivot_targets.clear()

    def choose_pair(self, bt_model, blocked=None):
        """
        Pick the next pair to play.
        bt_model: BTModel instance (for info-max scoring in greedy phase).
        blocked: set of frozenset pairs to exclude (in-flight / banned).

        Returns (first, second) or None if no pair available.
        """
        blocked = blocked or set()
        active = self.active_list
        if len(active) < 2:
            return None

        # Pivot mode overrides everything
        if self.pivot_mode != PivotMode.NONE and self.pivot_targets:
            pair = self._pick_pivot(bt_model, blocked)
            if pair:
                return pair
            # Fall through if no pivot pair available

        # Priority 1: RR needs (images in NEW or RR_ACTIVE)
        pair = self._pick_rr(active, blocked)
        if pair:
            return pair

        # Priority 2: ε-greedy info-max
        return self._pick_greedy(bt_model, active, blocked)

    def current_phase_label(self):
        """Human-readable description of the current scheduler state."""
        if self.pivot_mode == PivotMode.RR:
            names = [k.split("\\")[-1][:15] for k in sorted(self.pivot_targets)]
            return f"Pivot RR: {', '.join(names)}"
        if self.pivot_mode == PivotMode.GREEDY:
            names = [k.split("\\")[-1][:15] for k in sorted(self.pivot_targets)]
            eps_str = f"ε={self.pivot_epsilon}" if self.pivot_epsilon > 0 else "ε=0"
            return f"Pivot Greedy ({eps_str}): {', '.join(names)}"

        active = self.active_list
        phases = defaultdict(int)
        for k in active:
            phases[self.states[k].phase] += 1

        if phases.get(Phase.NEW, 0) > 0 or phases.get(Phase.RR_ACTIVE, 0) > 0:
            rr_done = phases.get(Phase.RR_DONE, 0) + phases.get(Phase.GREEDY, 0)
            return f"Round-Robin ({rr_done}/{len(active)} through RR)"
        return f"ε-Greedy (ε={self.epsilon:.2f})"

    # ------------------------------------------------------------------
    # Phase management
    # ------------------------------------------------------------------

    def _update_phases(self):
        """Promote images through phases based on current match data.
        v1.4.0: no tiebreaker step — RR_DONE promotes straight to GREEDY once
        the whole active field is through RR (TIEBREAK_DONE accepted for legacy)."""
        active = self.active_keys
        for key in active:
            s = self.states[key]
            if s.phase in (Phase.NEW, Phase.RR_ACTIVE):
                if s.rr_complete(active):
                    s.phase = Phase.RR_DONE
            if s.phase == Phase.RR_DONE:
                if all(self.states[k].phase.value in ("RR_DONE", "TIEBREAK_DONE", "GREEDY")
                       for k in active):
                    s.phase = Phase.GREEDY

    # ------------------------------------------------------------------
    # Pair selectors
    # ------------------------------------------------------------------

    def _pick_rr(self, active, blocked):
        """
        Find an image that still needs RR matches.
        Pick the one with fewest total matches (most needy).
        Play it against the opponent it's played least.
        """
        needy = []
        for key in active:
            s = self.states[key]
            if s.phase in (Phase.NEW, Phase.RR_ACTIVE):
                # Count how many RR matches still needed
                needed = 0
                for opp in active:
                    if opp == key:
                        continue
                    deficit = config.RR_CYCLES - s.pair_counts[opp]
                    if deficit > 0:
                        needed += deficit
                if needed > 0:
                    needy.append((needed, s.games, key))

        if not needy:
            return None

        # Sort by most RR matches needed, then fewest games (most starved)
        needy.sort(key=lambda x: (-x[0], x[1]))

        for _, _, key in needy:
            # Find the opponent this image has played LEAST among active
            s = self.states[key]
            opponents = []
            for opp in active:
                if opp == key:
                    continue
                played = s.pair_counts[opp]
                if played < config.RR_CYCLES:
                    opponents.append((played, self._rng.random(), opp))

            if not opponents:
                continue

            opponents.sort()
            for _, _, opp in opponents:
                pair = (key, opp) if self._rng.random() < 0.5 else (opp, key)
                if frozenset((key, opp)) not in blocked:
                    return pair

        return None

    def _pick_greedy(self, bt_model, active, blocked):
        """
        ε-greedy: with prob ε, pick a random pair.
        Otherwise, pick the pair with highest BT information score.
        """
        eps = self.epsilon
        if self.pivot_mode == PivotMode.GREEDY and self.pivot_targets:
            eps = self.pivot_epsilon

        if self._rng.random() < eps:
            # Random pair
            pool = list(active)
            self._rng.shuffle(pool)
            for i in range(len(pool)):
                for j in range(i + 1, len(pool)):
                    if frozenset((pool[i], pool[j])) not in blocked:
                        a, b = pool[i], pool[j]
                        return (a, b) if self._rng.random() < 0.5 else (b, a)
            return None

        # Greedy: score all pairs, pick best
        ci_widths = bt_model.confidence_intervals(self.active_keys)
        scored = []
        for i, key_a in enumerate(active):
            for key_b in active[i + 1:]:
                if frozenset((key_a, key_b)) in blocked:
                    continue
                score = bt_model.pair_information_score(key_a, key_b, ci_widths)
                scored.append((score, self._rng.random(), key_a, key_b))

        if not scored:
            return None

        scored.sort(reverse=True)
        # Pick from top-K for variety
        top_k = min(config.TOP_K_CANDIDATES, len(scored))
        _, _, key_a, key_b = scored[self._rng.randint(0, top_k - 1)]
        return (key_a, key_b) if self._rng.random() < 0.5 else (key_b, key_a)

    def _pick_pivot(self, bt_model, blocked):
        """Pick a pair involving a pivot target."""
        targets = sorted(t for t in self.pivot_targets
                         if t in self.states and not self.states[t].eliminated)
        non_targets = sorted(k for k in self.active_keys if k not in self.pivot_targets)

        if not targets or not non_targets:
            return None

        if self.pivot_mode == PivotMode.RR:
            # Round-robin: cycle each target against non-targets
            for target in targets:
                s = self.states[target]
                opponents = []
                for opp in non_targets:
                    if frozenset((target, opp)) not in blocked:
                        opponents.append((s.pair_counts[opp], self._rng.random(), opp))
                if opponents:
                    opponents.sort()
                    _, _, opp = opponents[0]
                    return (target, opp) if self._rng.random() < 0.5 else (opp, target)
            return None

        if self.pivot_mode == PivotMode.GREEDY:
            # Greedy: pair target with most uncertain opponent
            eps = self.pivot_epsilon
            if self._rng.random() < eps:
                # Random pivot pair
                target = self._rng.choice(targets)
                pool = [o for o in non_targets
                        if frozenset((target, o)) not in blocked]
                if pool:
                    opp = self._rng.choice(pool)
                    return (target, opp) if self._rng.random() < 0.5 else (opp, target)
            else:
                ci_widths = bt_model.confidence_intervals(self.active_keys)
                scored = []
                for target in targets:
                    for opp in non_targets:
                        if frozenset((target, opp)) in blocked:
                            continue
                        score = bt_model.pair_information_score(target, opp, ci_widths)
                        scored.append((score, self._rng.random(), target, opp))
                if scored:
                    scored.sort(reverse=True)
                    top_k = min(config.TOP_K_CANDIDATES, len(scored))
                    _, _, target, opp = scored[self._rng.randint(0, top_k - 1)]
                    return (target, opp) if self._rng.random() < 0.5 else (opp, target)
            return None

        return None

    # ------------------------------------------------------------------
    # State export (for dashboard / JSONL resume)
    # ------------------------------------------------------------------

    def export_state(self):
        """Export scheduler state as a JSON-serializable dict."""
        return {
            "step": self.step,
            "pivot_mode": self.pivot_mode.value,
            "pivot_targets": list(self.pivot_targets),
            "pivot_epsilon": self.pivot_epsilon,
            "images": {
                key: {
                    "phase": s.phase.value,
                    "eliminated": s.eliminated,
                    "elim_step": s.elim_step,
                    "games": s.games,
                    "wins": s.wins,
                    "losses": s.losses,
                    "last_played_step": s.last_played_step,
                }
                for key, s in self.states.items()
            }
        }

    def reconstruct_from_log(self, battles_jsonl_path):
        """
        Rebuild scheduler state (pair counts, phases) by replaying a battles.jsonl.
        Does NOT recompute theta/BT — that's the BTModel's job.
        """
        import json

        # Reset all match data but keep registered images
        for key in list(self.states.keys()):
            old_elim = self.states[key].eliminated
            old_elim_step = self.states[key].elim_step
            self.states[key] = ImageState(key)
            self.states[key].eliminated = old_elim
            self.states[key].elim_step = old_elim_step

        self.step = 0
        try:
            with open(battles_jsonl_path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    rec = json.loads(line)
                    a, b = rec["first"], rec["second"]
                    winner = rec.get("winner")
                    tie = rec.get("tie", False)

                    # Register any unknown images
                    self.add_image(a)
                    self.add_image(b)

                    self.step += 1
                    sa, sb = self.states[a], self.states[b]
                    sa.pair_counts[b] += 1
                    sb.pair_counts[a] += 1
                    sa.last_played_step = self.step
                    sb.last_played_step = self.step
                    if not tie and winner:
                        sa.decisive_counts[b] += 1
                        sb.decisive_counts[a] += 1
                        sa.games += 1
                        sb.games += 1
                        if winner == a:
                            sa.wins += 1
                            sb.losses += 1
                        else:
                            sb.wins += 1
                            sa.losses += 1
        except FileNotFoundError:
            pass

        self._update_phases()
