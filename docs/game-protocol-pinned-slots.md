# Pinned-Slot Game Protocol — Design Discussion

**Date:** 2026-08-14
**Project:** VLM-imageArena
**Status:** IMPLEMENTED 2026-08-14 (config v3: GAME_* settings in src/config.py, arena 3.0.0+). Design discussion preserved below.
**Author:** Anstein Huynh (concept) + Neru (writeup)
**Supersedes/extends:** `highlow-scheduler-design.md` §3 (split-order, deferred), FIXED_QUESTION decision

---

## 1. The problem this solves

Two facts from measurement (2026-08-14, Qwen3.6 era — remeasure on Qwen3.8):

1. **The prompt cache is whole-prompt.** A repeat query is warm (~165ms,
   ~1346 tokens cached). ANY change — image, image *order*, or question —
   is a full cold re-encode (~570ms, ~131 tokens cached).
2. **A single fixed question has prompt-specific blind spots.** The old
   FIXED_QUESTION=true design chose warmth over question variety. A phrasing
   that the judge reads oddly biases every match equally, invisibly.

Current protocol `[o1,o1,o2,o2]` pays 2 colds per 4 queries to counterbalance
position bias inside each match. This proposal instead **pins each slot to a
fixed (order × question) variant** so repeats are warm, and gets bias
cancellation + question variety from the *combination of slots*.

---

## 2. Vocabulary

| Term | Meaning |
|---|---|
| **Game** | One image-pair battle. The scheduling unit that produces one record in battles.jsonl. |
| **Variant** | One (image order × question) combination shown on one slot. |
| **Round** | One query fired by each participating slot (one "beat" of the game). |
| **Reps** (R) | The configurable variable: consecutive repetitions of the same variant on the same slot. First rep is cold, the remaining R−1 are warm. Aka "dwell rounds". |
| **Questions per game** (Q) | How many distinct questions (drawn from the prompt pool) a game uses. |

## 3. Core design

A game = one image pair. Each participating slot is pinned to one variant:

```
Slot1 pinned: [image1][image2][Question1]     (order AB × Q1)
Slot2 pinned: [image1][image2][Question2]     (order AB × Q2)
Slot3 pinned: [image2][image1][Question1]     (order BA × Q1)
Slot4 pinned: [image2][image1][Question2]     (order BA × Q2)
```

Slots fire queries; each stays on its variant for R rounds, then rotates to
the next question for its order. Anstein's worked example (Q=3, R=2):

```
Round 1:  S1:[1,2,Q1]  S2:[1,2,Q2]  S3:[2,1,Q1]  S4:[2,1,Q2]   (4 colds — game setup)
Round 2:  S1:[1,2,Q1]  S2:[1,2,Q2]  S3:[2,1,Q1]  S4:[2,1,Q2]   (all warm)
Round 3:  S1:[1,2,Q3]  S2:→next pair S3:[2,1,Q3]  S4:→next pair (2 colds — Q3 rotation + pipeline)
Round 4:  S1:[1,2,Q3]  S2:[next]     S3:[2,1,Q3]  S4:[next]     (S1/S3 warm)
```

Total for the pair: every variant seen exactly R times —

```
2× [i1][i2][Q1]   2× [i1][i2][Q2]   2× [i1][i2][Q3]
2× [i2][i1][Q1]   2× [i2][i1][Q2]   2× [i2][i1][Q3]
```

**The formula:**

```
QUERIES_PER_GAME = Q × 2 orders × R
```

(3 questions × 2 orders × R=2 → 12 queries per game.)

### Naming the configurable variable

Anstein's "the 2x": proposed canonical name **`REPS`** (repetitions per
variant) — statistical, reads naturally as "best-of-N" style sampling.
Alternative considered: **`dwell`** (rounds a slot dwells on one variant —
the cache-flavored reading). Recommend `REPS` in config:
`GAME_QUESTIONS = 3`, `GAME_REPS = 2`.

---

## 4. Synchronization: barrier or free-run?

Anstein's open question: *[wait until all slots are done or proceed
automatically?]*

**Recommendation: free-run (proceed automatically), with game-boundary gates.**

- A hard round barrier couples all slots to the slowest one (one cold query
  stalls 3 warm slots). Nothing statistically requires lockstep — warmth is
  per-slot private, so each slot can stream its variant list independently.
- The pipelined example above (S2/S4 rolling onto the next pair while S1/S3
  finish Q3) is exactly free-run: a shared **game queue** hands each slot its
  next (pair, variant) when it's ready.
- The only synchronization point that matters: **a game completes when all
  its queries are done** → write ONE record → run the elimination gate →
  update BT. Never gate mid-game: individual rounds are not balanced samples
  (each slot is voting on a different question), only the completed game is.

## 5. Statistical properties

- **Position bias cancels per question.** Every question is shown in both
  orders, R times each. γ=+0.436 (measured, old judge) cancels within every
  question's subscore — stronger than the old design, where cancellation
  happened per match for ONE question only.
- **Per-question subscores come free.** Each question yields its own
  counterbalanced mini-verdict (2R votes). Divergent subscores (Q1 favors X,
  Q2 favors Y) = prompt-sensitivity detector — data the old design could
  never see. Worth surfacing in the dashboard/battles record.
- **Seeds stay sequential and unique** (2.3.1 resume rule applies): each
  query draws a fresh seed, so R reps of a variant are R independent samples
  from P(X wins | that exact prompt). Identical (prompt, seed) never refires.

## 6. Cost accounting (why R is the warmth dial)

Every variant costs exactly **one cold** (its first rep); the other R−1 reps
are warm. Per game:

```
colds = 2Q (one per variant)     warms = 2Q(R−1)
avg query latency ≈ (570 + (R−1)·165) / R      [old-model ms; remeasure for 3.8]
```

| R | avg/query | total for Q=3 | notes |
|---|---|---|---|
| 1 | ~570ms | 24 queries ≈ 13.7s | all cold — max variety, zero warmth |
| 2 | ~367ms | 12 queries ≈ 4.4s | Anstein's example |
| 4 | ~247ms | 24 queries ≈ 5.9s | same total as R=1 for +2.3s |
| ∞ (Q=1) | →165ms | — | old FIXED_QUESTION hammer, max warmth, zero variety |

- **Q buys robustness** (question decorrelation) at a fixed 2-cold tax per
  extra question (~1.1s old-model).
- **R buys warmth** and more samples per variant — statistically the
  per-variant vote share tightens like a binomial.
- Question subsets should **rotate across games** (draw Q from the 20-prompt
  pool per game) so different games probe different phrasings — cross-game
  decorrelation for free.

## 7. What changes in the codebase (if adopted)

1. `config.py`: `GAME_QUESTIONS`, `GAME_REPS`, retire FIXED_QUESTION path.
2. New **game manager**: builds the variant list per game (Q questions × 2
   orders), assigns variants to slots, owns the game queue, writes one
   battle record per game (with per-question subscores + per-query
   order/question/seed/slot fields).
3. `arena.py` workers become variant-stream runners instead of
   pair-choosers.
4. Server: enable `--cache-idle-slots` (protects pinned caches when a slot
   idles between games). `--slot-save-path` remains NOT useful (restore +
   re-query measured fully cold).
5. Scheduler gates (HighLow Z-check etc.) move to game boundaries.

## 8. Open questions

- ~~Which phase uses which (Q, R)?~~ → **decided, see §10**
- Does an odd slot count break the 2-order symmetry? (5 slots → 3+2 split —
  accept small bias, or drop to 4.)
- Remesure cold/warm/cached-token numbers on Qwen3.8-27B before committing
  to Q/R values (§1 numbers are Qwen3.6-era). → empirical test running
  2026-08-14, results in `ai/test/20260814-game-protocol-assumptions/FINDINGS.md`
- Dashboard: how to display per-question subscores in the match feed.

## 9. Related

- `docs/highlow-scheduler-design.md` — elimination scheduling this plugs into
- `ai/test/20260814-ctx-sweep/` — the cache measurements (§1)
- `ai/test/20260814-slot-save-restore/FINDINGS.md` — why slot save/restore
  doesn't help rotation

## 10. Anstein's decisions + assumptions registry (2026-08-14 18:47)

### Decisions

- **(Q, R) applies to ALL phases** — every phase uses the game protocol; the
  phases differ only in how many reps they get (later phases = more reps).
- **HighLow is RETIRED.** It was a bandaid for slow cold queries. With the
  pinned-slot protocol making queries cheap, the scheduler reverts to
  **FullRR → GreedyEpsilon** (the adaptive pre-HighLow design) — the
  statistical gate (θ-gap > Z·SE, checked at game boundaries) stays as the
  match-exit criterion (REPS is a cap, not a target).
- The architectural bet, explicitly: **cache reuse is so cheap that we can
  afford to run LOTS of queries** — making the old elimination-efficiency
  machinery (1/δ² query optimization) unnecessary. Empirical verification
  of "how cheap" is in progress (see §11).

### Assumptions this architecture rests on (verify each)

1. **Slot isolation** — slot 1-4 caches are independent; one slot's activity
   never degrades another slot's cache.
2. **Seed-change is near-free** — a slot already warm on a prompt can fire
   a new seed essentially instantly (only the sampled tokens change).
3. **Scheduler competence** — we can write a game manager that pipelines
   slots ("Slot2 [processing next img pair]" while others finish) without
   deadlock/starvation.
4. **Idle-slot handling** — we know how to direct slots that finish early
   (the "[wait until all slots are done]" question; free-run + game-boundary
   gates is the current answer).
5. **Lookahead requirement** — the next game must be chosen while the
   current game is still evaluating (no idle gap between games).
6. **Server-side cache helpers pay off** — one or more of `--cache-idle-slots`,
   `--slot-save-path`, `POST /slots/{id}?action=save|restore` measurably
   improves speed.
7. *(added)* **Single-entry vs multi-entry cache per slot** — if a slot's
   cache only holds the LAST prompt, alternating variants on one slot
   re-colds every switch (kills some pipelining patterns).
8. *(added)* **Warm queries parallelize** — 4 concurrent warm queries don't
   contend badly (old model had a ~62ms serial CPU component; the dense 27B
   may differ).
9. *(added)* **Vote entropy across seeds** — reps are only informative if
   different seeds on the same prompt actually produce different votes
   (non-degenerate distribution).
10. *(added)* **Position bias magnitude on the new judge** — the per-question
    counterbalance argument needs γ known for Qwen3.8 (γ=+0.436 was the old
    judge).
11. *(added)* **Idle retention** — a slot's cache survives idle gaps between
    games without `--cache-idle-slots` (or the flag fixes it if not).

## 11. Empirical verification

Assumptions 1, 2, 6, 7, 8, 9, 10, 11 were tested against the live 27B judge
(2026-08-14, isolated run). Full tables:
`ai/test/20260814-game-protocol-assumptions/FINDINGS.md`.
Assumptions 3-5 (scheduler design) are on us — addressed by the game-manager
state machine in §4/§7.

### Results snapshot (Qwen3.8-27B Q5_K_M, np=4, cache-ram 8192)

| # | Assumption | Verdict | Key number |
|---|---|---|---|
| 1 | Slot isolation | ✅ HOLDS | slot0 cache untouched through 3 rounds of other-slot colds |
| 2 | Seed-change free | ✅ HOLDS | 20/20 seeds fully warm; warm = **212 ms median** (p95 214) |
| — | Cold baseline | — | **~1124 ms** (cached=131) → 5.3× warm speedup |
| — | RAM-restore tier | — | 380–600 ms (full tokens cached, VRAM copy evicted) — a third latency tier between warm and cold |
| 7 | Multi-entry cache/slot | ✅ HOLDS* | A,B,A,B ping-pong: tokens stay cached, but latency lands in the RAM-restore tier (303–604 ms) — keep a slot on ONE variant per rep-group |
| 11 | Idle retention | ✅ HOLDS | 30s AND 90s idle → still fully cached; `--cache-idle-slots` supported (default ON) |
| 6 | save/restore endpoints | ❌ N/A | HTTP 501 — requires `--slot-save-path` at launch; not needed (T4/T5 make it moot) |
| 8 | Warm queries parallelize | ⚠️ WEAK | 4-slot warm aggregate = **5.46 QPS** vs 4.71 sequential (+16%) — **GPU-bound, not cache-bound**: slots don't contend, the GPU does |
| 10 | Position bias γ (new judge) | ✅ ~ZERO | **50.0% position-2** (n=50; old judge 60.6%); X won 76% in BOTH orders — votes track the image, not position |
| 9 | Vote entropy | ✅ HOLDS | 15–24% flip rate across seeds on identical prompts — reps are informative |

### What the numbers imply

- **GAME_REPS R = 4–8**: first rep of a variant ~1.1s cold (or RAM-restore),
  every rep after ~212 ms. R=4 ≈ 1.7 s/game, R=8 ≈ 3.4 s (sequential).
- **The cache-cheapness bet holds per-query but hits a GPU ceiling**: 5.46 QPS
  aggregate no matter how slots are arranged. FullRR with Q=3, R=2 on 19
  images = 171 pairs × 12 = 2,052 queries ≈ **6.3 min** — still ~3× faster
  than the measured 2026-08-14 run (2,108 queries in 1,199 s = 1.75 QPS).
  More slots won't help this dense model; the lever is quantization/model.
- **γ≈0 weakens (but doesn't void) the 2-order symmetry requirement**:
  keeping both orders costs nothing extra (multi-entry cache), so the
  insurance stays free. n=50 is small — re-verify γ occasionally.
- Pipelining rule from T4/T7: keep each slot on ONE variant for its full rep
  group (alternating variants per slot pays the restore tier every switch).
