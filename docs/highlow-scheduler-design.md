# HighLow Elimination Scheduler — Design Doc

**Date:** 2026-08-14
**Project:** VLM-imageArena
**Status:** Implemented 2026-08-14, then RETIRED the same day — the pinned-slot game protocol (game-protocol-pinned-slots.md) made queries cheap enough that HighLow was unnecessary. Kept as design record.
**Author:** Neru (assistant) + Anstein Huynh

---

## 1. Goal

Rank N candidate images (headshots, logos, etc.) to a confident top-K as fast as
possible. The current scheduler (adaptive BT-MLE + phase machine) produces
*overnight* runtimes. This redesign shrinks that to **minutes** by exploiting the
VLM judge's prompt cache.

**Deliverable:** the top ~5% of images, with sharpened confidence intervals, for
Anstein to eyeball and pick from.

---

## 2. The core insight (measured, not assumed)

The llama.cpp judge's prompt cache (`--cache-prompt`) is **whole-prompt**:

| query type | latency | `tokens_cached` |
|---|---|---|
| warm (identical prompt repeated) | **~165 ms** | 1346 (everything) |
| any change (image *or* question) | **~570–700 ms** | 131 (system prompt only) |

- There is **no content-hash vision-embedding cache** — re-using one image inside
  a *new* prompt does not help. (A "pos1 cheap / pos2 expensive" asymmetry we
  briefly thought we found was a RAM-cache artifact of alternating only 2 prompts.)
- The expensive part of a cold change is the **vision encode + re-prefill of
  ~1232 tokens** (both images' embedding tokens + text) at ~0.4 ms/token.

**Therefore: encode each pair once, then hammer it warm.** Speed comes from
*repeating the same prompt*, not from clever pair selection. That single fact
dictates the scheduler shape.

### Cost model (why the rotation is not the problem)

An elimination = hammer one pair ~30–66 times to 95% CI, *then* rotate once:

- Hammering (warm, ~165 ms × ~66 queries) ≈ **~8 s** at np12.
- Rotation (one cold ~570 ms encode) ≈ **~5%** of the elimination cost.

So the warm hammering dominates. The rotation is a sliver — this is why
save/restore (which would only avoid the rotation) is *not* the lever.

---

## 3. The HighLow scheduler

The cycle (repeats until the top-K survives):

```
RR (rr_warmup rounds) → TB → GE → HighLow × ceil(N × highlow_batch_frac)
RR (rr_regular rounds) → TB → GE → HighLow × ceil(N × highlow_batch_frac)
... repeat, halving the field each cycle
```

- **RR (round robin):** every image plays every other ≥`rr_warmup` times
  (`RR_DONE >= rr_warmup`). Establishes a baseline θ. This is O(N²) and *cold*,
  so it is the expensive warmup — the reason `rr_warmup` is small and the re-RR
  in later cycles is a near-no-op (θ is cumulative from the battle log).
- **TB (tie breaker):** resolve pairs with zero decisive results. Effort scaled by
  `tb_warmup`, a float [0,1] (1.0 = full tiebreak to `rr_warmup` rounds, 0.5 = half).
- **GE (greedy epsilon):** ε-greedy info-max pair selection, `eg_warmup` rounds.
  Sharpens θ — especially identifies the leader and the worst for HighLow.
- **HighLow:** **leader vs worst.** Pin the leader and the current-worst, hammer
  until θ-gap > `highlow_ci` (95%), eliminate the worst. Repeat until
  `highlow_batch_frac` of the remaining batch is gone.

### Why leader-vs-worst

- **Max δ → cheapest elimination.** Elimination queries scale ~1/δ²: δ=0.5 → ~66
  queries, δ=0.1 → ~812 queries (at ~39% tie rate, 95% CI). Leader-vs-worst is the
  largest gap in the field, so each elimination is as cheap as possible.
- **The leader stays warm.** Pin the leader at a fixed slot position across the
  whole HighLow phase; only each new worst pays one cold encode.
- **Self-correcting.** Even if the leader is only mid-pack, we still correctly
  cull the bottom (just with slightly smaller δ).

### Bias handling (mandatory)

The judge has a **strong position bias: γ = +0.436** (60.6% "B" = second-image
preference). Counterbalancing is non-negotiable. Use **split-order**: half the
slots pin `[leader,worst]`, half pin `[worst,leader]`. Each slot stays warm on its
fixed order, and the bias cancels in the aggregate θ. (Alternative: fit γ once and
debias single-order queries — fewer slots needed, but requires calibration.)

### Question handling

The question string must be **fixed per pair** (or rotate among a tiny fixed set
that all stay in the 8 GB RAM cache). A *unique* question every query = a full
~570 ms re-encode every query.

---

## 4. Config knobs

| Knob | Default | Meaning |
|---|---|---|
| `rr_warmup` | 2 | full RR passes at start (`RR_DONE >= rr_warmup`) |
| `tb_warmup` | 1.0 | tiebreaker effort, float [0,1], clamped |
| `eg_warmup` | 30 | greedy-epsilon rounds at start |
| `rr_regular` | 1 | RR passes in repeat cycles |
| `tb_regular` | 1 | tiebreaker effort in repeat cycles |
| `eg_regular` | 30 | greedy-epsilon rounds in repeat cycles |
| `highlow_ci` | 0.95 | elimination confidence (θ-gap > z·SE) |
| `highlow_batch_frac` | 0.5 | fraction of remaining batch eliminated per HighLow |
| `top_k_frac` | 0.05 | deliverable: top ~5% |

---

## 5. Hard constraints (user decisions — do not override)

- **KV quantization forbidden** (fp16 KV only).
- **MTP off** (`--spec-type none` — measured pathological: 100% GPU, no output
  for minutes on a 1-token A/B answer).
- **768² production resolution.**
- **Soft elimination** (bench, don't delete — reversible).
- **Sequential seeds** (query 1 = seed 1, query 2 = seed 2, …).
- **Judge config:** ctx 100000, np 12, ARENA_CONCURRENCY 12, threads 8.
  (Measured: 100k ctx fits at fp16 because KV is lazily allocated under
  `--no-warmup`; np 12 is the QPS sweet spot before CPU contention dominates.)

---

## 6. Empirical findings (the numbers that justify this)

### Cache warm vs cold (768², np 12)

| mode | q/s | latency | cache hit | GPU |
|---|---|---|---|---|
| **pinned (warm)** | **12.8** | 727 ms | 100% | 41% |
| bounce (cold) | 3.1 | 3713 ms | 53% | 60% |

→ **4.1× from keeping the cache warm.** Baseline production was ~0.96 q/s, so
this is the dominant win.

### Warm-query cost decomposition (np 1)

| prompt | latency |
|---|---|
| text-only | 97 ms |
| 1 image | 132 ms |
| 2 images | 168 ms |

→ The vision encode is the *only* thing that makes a query slow; the LLM prefill
is ~35 ms per image.

### Slot sweep (warm QPS plateaus ~14, CPU-bound)

| np | 4 | 6 | 8 | 10 | 12 | 16 |
|---|---|---|---|---|---|---|
| q/s | 10.4 | 11.2 | 11.7 | 12.0 | 13.1 | 14.1 |

→ QPS caps ~16 (a ~62 ms serial CPU component per query); GPU sits ~44%. Not GPU-bound.

### Levers tested (results)

| lever | result |
|---|---|
| `--threads 16` | worse (12.35 vs 13.14) — 8 threads = 8 cores, already optimal |
| `draft-mtp` | pathological (hang). Keep `none` |
| file:// (`--media-path`) | old build: no cache. **b10433: caches** — warm 138–144 ms vs 164 ms base64 (~14% win). Needs PNG on disk (`.webp` silently dropped) |
| slot save/restore | works on b10433 (200) but restore + re-query = cold (`cached=0`). Only restart-persistence of exact repeats; does NOT help rotation |
| llama.cpp b10213 → b10433 | rotation cost unchanged (~570–660 ms) |
| resolution 768→256 | +32% QPS (modest; floor is fixed overhead) |

### Elimination cost vs strength gap δ (39% tie rate, 95% CI)

| δ | 0.1 | 0.2 | 0.5 | 1.0 |
|---|---|---|---|---|
| median queries | 812 | 280 | 66 | 26 |

→ Eliminate well-separated losers first (cheap); fine-grain only the top cluster.

---

## 7. Implementation plan

1. **`config.py`** — add the §4 knobs; set judge config to ctx 100000 / np 12 /
   threads 8 (fp16 KV, spec-type none).
2. **New scheduler mode** (extend `scheduler.py`, don't rewrite the BT engine):
   - Add `HighLow` phase: leader-vs-worst pair selection, split-order slot pinning.
   - Add a "batch elimination" loop (eliminate `highlow_batch_frac` per cycle).
   - Elimination predicate: `θ_gap > z·SE` using the existing Fisher CI /
     `pairwise_gap_se`.
   - Soft-eliminate (mark benched, exclude from pair selection, keep record).
3. **`judge.py`** — sequential seeds; fixed question per pair; optional split-order
   via `id_slot` pinning (already supported).
4. **Optional dials (later):** file:// + PNG-on-disk (~14% warm win); resolution
   tiering for the coarse cull (512²/256² cull, 768² fine-rank).

---

## 8. Open questions / deferred

- **Split-order vs γ-debias** — split-order is the robust default; γ-debias
  (fit once, single-order queries) is a later optimization.
- **Question-first ordering** (`sys,q,img1,img2`) measured a partial-prefix reuse
  (Δimg2 549→369 ms in one test) — worth re-confirming as a possible layout tweak,
  but not required for v1.
- **`highlow_batch_frac` tuning** — 0.5 is the starting point; smaller fractions
  are safer (less risk of a noisy mis-drop), larger are faster.
- **Seed behavior on resume** — ensure the sequential seed counter survives
  log replay.

---

## 9. Related docs / test artifacts

- `docs/multimodal-two-images-in-one-message.md` — the second-image-drop gotcha
  (motivates the fixed question + separate-message layout).
- Test harness + all raw results: `<local test dir>/20260814-ctx-sweep/`
- Slot save/restore findings: `<local test dir>/20260814-slot-save-restore/FINDINGS.md`
- Session memory: `<session memory> 20260814-VLM-imageArena-ctx-sweep`,
  `<session memory> 20260814-VLM-imageArena-qps-max`
