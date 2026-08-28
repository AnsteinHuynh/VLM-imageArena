"""
VLM-imageArena — Bradley-Terry MLE rating engine.
Version: 1.0.0

Order-independent batch rating: all match results are fit simultaneously.
No sequential updates, no sigma dynamics, no path dependency.

Zermelo's algorithm (iterative MM) for the MLE fit.
Fisher information for confidence intervals (fast, deterministic).
Bootstrap available on demand (slower, more robust for small samples).
"""

VERSION = "1.2.0"

import math
from collections import defaultdict


class BTModel:
    """
    Bradley-Terry model fit via Zermelo iteration.

    Attributes:
        players: list of player keys (image paths)
        idx: {player_key: index}
        theta: log-strength vector (sum = 0)
        wins_matrix: wins_matrix[i][j] = times i beat j (decisive only)
        games: {player_key: total decisive games played}
        decisive_matches: total non-tie matches recorded
    """

    def __init__(self):
        self.players = []
        self.idx = {}
        self.theta = []
        self._wins = None  # 2D list, _wins[i][j]
        self.games = defaultdict(int)
        self.decisive_matches = 0
        self._dirty = True

    # ------------------------------------------------------------------
    # Recording matches
    # ------------------------------------------------------------------

    def add_player(self, key):
        """Register a new player. Safe to call multiple times."""
        if key not in self.idx:
            self.idx[key] = len(self.players)
            self.players.append(key)
            self.theta.append(0.0)
            # Grow the wins matrix if it already exists
            if self._wins is not None:
                n = len(self._wins)
                for row in self._wins:
                    row.append(0)
                self._wins.append([0] * (n + 1))
            self._dirty = True

    def record_match(self, winner, loser):
        """Record one decisive match (ties are NOT recorded here)."""
        self.add_player(winner)
        self.add_player(loser)
        wi, li = self.idx[winner], self.idx[loser]
        if self._wins is None:
            self._rebuild_matrix()
        self._wins[wi][li] += 1
        self.games[winner] += 1
        self.games[loser] += 1
        self.decisive_matches += 1
        self._dirty = True

    def replay_log(self, battles_jsonl_path):
        """Load all decisive observations from a battles.jsonl file.

        v3 game-protocol records (arena >= 3.0.0) carry per-query votes in
        `queries` — each decisive query vote counts as ONE observation,
        matching the live path (bt.record_match per query). Legacy records
        (no `queries` field, or all-malformed) fall back to one observation
        per line via `winner`. Mixing both in one log is fine.
        """
        import json
        self._wins = None
        self.games = defaultdict(int)
        self.decisive_matches = 0
        # Keep registered players, reset their data
        for p in self.players:
            self.games[p] = 0

        def _observe(self_, winner, loser):
            # Ensure both players are registered (matrix grows automatically)
            self.add_player(winner)
            self.add_player(loser)
            if self._wins is None:
                self._rebuild_matrix()
            wi, li = self.idx[winner], self.idx[loser]
            self._wins[wi][li] += 1
            self.games[winner] += 1
            self.games[loser] += 1
            self.decisive_matches += 1

        try:
            with open(battles_jsonl_path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    rec = json.loads(line)
                    queries = rec.get("queries")
                    if queries:
                        for qu in queries:
                            letter = qu.get("letter")
                            if letter not in ("A", "B"):
                                continue
                            order = qu.get("order") or [rec["first"], rec["second"]]
                            winner = order[0] if letter == "A" else order[1]
                            loser = order[1] if letter == "A" else order[0]
                            _observe(self, winner, loser)
                    elif not rec.get("tie"):
                        winner = rec["winner"]
                        loser = rec["first"] if winner == rec["second"] else rec["second"]
                        _observe(self, winner, loser)
        except FileNotFoundError:
            pass  # fresh tournament, no log yet
        self._dirty = True

    def _rebuild_matrix(self):
        """Allocate the wins matrix for the current player set."""
        n = len(self.players)
        self._wins = [[0] * n for _ in range(n)]

    # ------------------------------------------------------------------
    # Fitting (Zermelo MM algorithm)
    # ------------------------------------------------------------------

    def fit(self, active_players=None, iterations=500, tol=1e-10):
        """
        Fit theta via Zermelo iteration on the ACTIVE player set.
        Eliminated players are excluded; their match data is ignored.

        active_players: set of player keys to include. None = all registered.
        Results stored in self.theta (all players; inactive keep old values).
        """
        if self._wins is None:
            self._rebuild_matrix()
        all_n = len(self.players)
        if all_n == 0:
            return

        if active_players is None:
            active = list(range(all_n))
        else:
            active = [self.idx[p] for p in self.players if p in active_players]
        n = len(active)
        if n < 2:
            for i in active:
                self.theta[i] = 0.0
            return

        # Build active-only views
        wins = [0.0] * n  # wins[i] = total wins of active[i] vs other active
        for ii, i in enumerate(active):
            for jj, j in enumerate(active):
                if i != j:
                    wins[ii] += self._wins[i][j]

        theta = [0.0] * n
        for _ in range(iterations):
            denom = [0.0] * n
            for ii in range(n):
                i = active[ii]
                for jj in range(ii + 1, n):
                    j = active[jj]
                    c = self._wins[i][j] + self._wins[j][i]
                    if c == 0:
                        continue
                    ei = math.exp(theta[ii])
                    ej = math.exp(theta[jj])
                    d = 1.0 / (ei + ej)
                    denom[ii] += c * d
                    denom[jj] += c * d
            max_delta = 0.0
            new_theta = [0.0] * n
            for ii in range(n):
                if denom[ii] < 1e-12:
                    new_theta[ii] = 0.0
                else:
                    ratio = wins[ii] / denom[ii]
                    v = math.log(ratio) if ratio > 1e-9 else -6.0
                    v = max(-6.0, min(6.0, v))
                    new_theta[ii] = v
                max_delta = max(max_delta, abs(new_theta[ii] - theta[ii]))
            theta = new_theta
            if max_delta < tol:
                break

        # Center: sum(theta) = 0
        mean = sum(theta) / n
        for ii, i in enumerate(active):
            self.theta[i] = max(-6.0, min(6.0, theta[ii] - mean))

        self._dirty = False

    # ------------------------------------------------------------------
    # Confidence intervals (Fisher information)
    # ------------------------------------------------------------------

    def bootstrap_ci(self, active_players=None, n_iter=100, seed=0):
        """
        Bootstrap 90% confidence interval half-widths (numpy, fast).

        Resamples the decisive matches with replacement, re-fits the BT model
        each time, and takes the 5–95 percentile spread as the CI half-width.
        Robust where the Fisher-information matrix is ill-conditioned (uneven
        games per player). This is the method used in the standalone analysis
        (bt_mle.py) that produced clean ±0.4 intervals.

        Returns {player_key: ci_half_width} for active players.
        """
        try:
            import numpy as np
        except ImportError:
            return self.confidence_intervals(active_players)

        if self._dirty:
            self.fit(active_players)

        if active_players is None:
            active = list(range(len(self.players)))
        else:
            active = [self.idx[p] for p in self.players if p in active_players]
        n = len(active)
        if n < 2:
            return {self.players[i]: 0.0 for i in active}

        # Collect decisive matches as (winner_local, loser_local) index pairs
        g_to_a = {g: a for a, g in enumerate(active)}
        pairs = []
        for gi in active:
            for gj in active:
                if gi == gj:
                    continue
                c = self._wins[gi][gj]
                for _ in range(c):
                    pairs.append((g_to_a[gi], g_to_a[gj]))
        if not pairs:
            return {self.players[i]: 0.0 for i in active}
        pairs = np.array(pairs, dtype=np.int64)
        n_pairs = len(pairs)

        rng = np.random.default_rng(seed)
        thetas = np.zeros((n_iter, n))
        for it in range(n_iter):
            idx = rng.integers(0, n_pairs, size=n_pairs)
            sampled = pairs[idx]
            W = np.zeros((n, n))
            np.add.at(W, (sampled[:, 0], sampled[:, 1]), 1)
            thetas[it] = self._zermelo_np(W, n)

        lower = np.percentile(thetas, 5, axis=0)
        upper = np.percentile(thetas, 95, axis=0)
        ci = {}
        for a, g in enumerate(active):
            ci[self.players[g]] = float((upper[a] - lower[a]) / 2.0)
        return ci

    @staticmethod
    def _zermelo_np(W, n, iterations=150):
        """Vectorized Zermelo iteration (numpy) for bootstrap refits."""
        import numpy as np
        w = np.ones(n)
        wins = W.sum(axis=1)
        N = W + W.T  # symmetric match-count matrix
        for _ in range(iterations):
            wsum = w[:, None] + w[None, :]  # w_i + w_j
            denom = (N / wsum).sum(axis=1)
            w_new = np.where(denom > 1e-12, wins / denom, 0.0)
            w_new = np.clip(w_new, 1e-9, 1e9)
            if np.max(np.abs(w_new - w)) < 1e-8:
                w = w_new
                break
            w = w_new
        theta = np.log(np.clip(w, 1e-9, 1e9))
        theta = theta - theta.mean()
        return np.clip(theta, -6.0, 6.0)

    def _fisher_cov(self, active):
        """Full n×n Fisher info matrix -> covariance (pinv). Returns (keys, cov).

        active: ordered list of player indices (into self.players).
        keys:   player keys in the same order as cov's rows/cols.
        cov:    n×n covariance matrix (Moore-Penrose pseudo-inverse of the
                info matrix; the single null direction — the uniform shift
                removed by sum-to-zero — is truncated).
        """
        import math as m
        n = len(active)
        I_full = [[0.0] * n for _ in range(n)]
        for ii in range(n):
            i = active[ii]
            for jj in range(n):
                j = active[jj]
                if ii == jj:
                    continue
                c = self._wins[i][j] + self._wins[j][i]
                if c == 0:
                    continue
                diff = self.theta[i] - self.theta[j]
                p = 1.0 / (1.0 + m.exp(-diff))
                pq = p * (1.0 - p)
                I_full[ii][jj] -= c * pq
                I_full[ii][ii] += c * pq
        cov = _invert_matrix(I_full, ridge=0)
        return [self.players[i] for i in active], cov

    def confidence_intervals(self, active_players=None):
        """
        Compute 90% confidence interval half-widths via Fisher information.

        Builds the full n×n info matrix and reads the per-player variance
        straight off the pseudo-inverse diagonal (the null direction — the
        uniform shift removed by sum-to-zero — is truncated by pinv).

        Returns {player_key: ci_half_width} for active players.
        ci_half_width = z * sqrt(variance), z=1.645 for 90% CI.
        """
        import math as m

        if self._dirty:
            self.fit(active_players)

        if active_players is None:
            active = list(range(len(self.players)))
        else:
            active = [self.idx[p] for p in self.players if p in active_players]
        n = len(active)
        if n < 2:
            return {self.players[i]: 0.0 for i in active}

        keys, cov = self._fisher_cov(active)
        z = 1.645  # 90% CI half-width
        ci = {}
        for ii in range(n):
            var = cov[ii][ii] if cov and ii < len(cov) else 1.0
            ci[keys[ii]] = z * m.sqrt(max(0.0, var))
        return ci

    def pairwise_gap_se(self, active_players=None):
        """
        Fisher standard error of the θ difference for every pair of active
        players: SE(θ_a − θ_b) = sqrt(Var_a + Var_b − 2·Cov_ab).

        Returns {frozenset((a, b)): se} for active players. Used for
        adjacent-rank significance in the ranking.
        """
        if self._dirty:
            self.fit(active_players)

        if active_players is None:
            active = list(range(len(self.players)))
        else:
            active = [self.idx[p] for p in self.players if p in active_players]
        n = len(active)
        if n < 2:
            return {}

        keys, cov = self._fisher_cov(active)
        se = {}
        for ii in range(n):
            for jj in range(ii + 1, n):
                var = cov[ii][ii] + cov[jj][jj] - 2.0 * cov[ii][jj]
                se[frozenset((keys[ii], keys[jj]))] = max(0.0, var) ** 0.5
        return se

    # ------------------------------------------------------------------
    # Pair scoring (for ε-greedy scheduler)
    # ------------------------------------------------------------------

    def pair_information_score(self, a, b, ci_widths=None):
        """
        Score a pair by expected information gain.
        Higher = more uncertain outcome = more informative match.

        score = P(A wins) * P(B wins) * (CI_A + CI_B)
        where P(A wins) = 1/(1+exp(-(theta_A - theta_B)))
        """
        ia, ib = self.idx.get(a), self.idx.get(b)
        if ia is None or ib is None:
            return 0.0
        diff = self.theta[ia] - self.theta[ib]
        p_a = 1.0 / (1.0 + math.exp(-diff))
        pq = p_a * (1.0 - p_a)
        if ci_widths:
            ci_sum = ci_widths.get(a, 0) + ci_widths.get(b, 0)
        else:
            ci_sum = 1.0  # uniform if no CI data
        return pq * ci_sum

    def win_probability(self, a, b):
        """P(a beats b) based on current theta estimates."""
        ia, ib = self.idx.get(a), self.idx.get(b)
        if ia is None or ib is None:
            return 0.5
        return 1.0 / (1.0 + math.exp(-(self.theta[ia] - self.theta[ib])))

    # ------------------------------------------------------------------
    # Queries
    # ------------------------------------------------------------------

    def get_theta(self, key):
        """theta for a single player (0.0 if unknown)."""
        i = self.idx.get(key)
        return self.theta[i] if i is not None else 0.0

    def get_games(self, key):
        """Decisive games played by key."""
        return self.games.get(key, 0)

    def ranking(self, active_players=None):
        """
        Return sorted [(key, theta, games)] descending by theta.
        Only active players included if active_players is provided.
        """
        if active_players is None:
            entries = [(self.players[i], self.theta[i], self.games.get(self.players[i], 0))
                       for i in range(len(self.players))]
        else:
            entries = [(p, self.get_theta(p), self.get_games(p))
                       for p in active_players]
        entries.sort(key=lambda x: x[1], reverse=True)
        return entries


# ----------------------------------------------------------------------
# Matrix inversion (SVD pseudo-inverse)
# ----------------------------------------------------------------------

def _invert_matrix(A, ridge=0.0):
    """Invert a square matrix robustly via SVD pseudo-inverse (Moore-Penrose).

    The Fisher information matrix for a flat / identical field is near-singular
    (measured condition number > 1e15), so a direct Gauss-Jordan inverse
    explodes to garbage (observed CI ~ 6.66 / 59.9). pinv truncates the
    (near-)null directions and returns the honest finite variance.

    ridge: optional diagonal (Tikhonov) regularizer added before inversion.
    """
    import numpy as np
    M = np.asarray(A, dtype=float)
    if ridge:
        M = M + np.eye(M.shape[0], dtype=float) * ridge
    return np.linalg.pinv(M, rcond=1e-12).tolist()
