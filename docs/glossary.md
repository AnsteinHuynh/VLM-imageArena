# VLM-imageArena — Glossary

**Date:** 2026-08-14 · **Applies to:** arena 3.1.0 / config 2.6.1 / scheduler 1.4.0 / bt_model 1.2.0 / dashboard 2.5.0
**Scope:** code-anchored terms only — every entry maps to something in the codebase
(`src/arena.py`, `src/config.py`, `src/bt_model.py`, `src/scheduler.py`, dashboard HTML).
Protocol design: `game-protocol-pinned-slots.md`.

---

## The unit chain

Everything is built from one atom:

| Term | In code | What it is |
|---|---|---|
| **Query** | `judge.query()` · one entry in `queries[]` of a battles.jsonl line | One HTTP POST to the judge: a single (image order, question, seed, slot) tuple. The atomic unit of work and of cost. |
| **Vote** | `letter` ("A"/"B") on a query; *credited* via order: A → `order[0]`, B → `order[1]` | The judge's answer to one query, mapped to an image. |
| **Decisive vote** | `credited is not None` → `bt.record_match(...)` | A vote that credited someone. Each one = one Bradley-Terry observation, applied **instantly mid-game**. The only thing that moves θ. |
| **Variant** | `GameSpec.variants` — `(order_tuple, q_index, question)` | One (image order × question) combination. Q questions × 2 orders = 2Q variants per game. **Pinned one-per-slot.** |
| **Rep** | `GAME_REPS` (cap) | One repeat of a variant on its slot. Rep 1 = cold, rep 2+ = warm. |
| **Game** | one line in `battles.jsonl`, one `game` number | One pair's battle: queries until a stop rule fires. Dashboard "Games" stat and feed `#N` count these. |
| **Battle / Match** | legacy words for game (file is still `battles.jsonl`) | Same thing as a game. Old protocol's fixed-4-query match. |

Query cost tiers on the current judge (Qwen3.8-27B Q5_K_M, 2 slots):

| Tier | Latency | Physical meaning |
|---|---|---|
| warm | ~212 ms | identical prompt already in this slot's KV cells |
| RAM-restore | ~380–600 ms | tokens accounted cached, but KV must be re-copied into VRAM (variant switch / revisit of an old prompt the slot remembers) |
| cold | ~1,124 ms | vision-encode of new image bytes + full prefill — **first contact with an image on that slot**; no layout avoids it |

## Stop rules (checked after every query, in order)

From `config.py` / `_worker` in arena 3.1.0:

1. **Unanimous** (`GAME_UNANIMOUS_EXIT`) — one image holds ALL credited votes, the other zero → game over (best-of-2 behavior for decisive pairs).
2. **Gap gate** (`GAME_EARLY_EXIT`, `GAME_GATE_Z=2.5`) — |Δθ| between the pair > Z × SE(gap) → decided, cut reps.
3. **SE floor** (`GAME_SE_FLOOR=0.15`) — SE(gap) below the floor AND still not decided → statistically indistinguishable → end as tie. (Calibrated from field data: SE(gap)≈0.15–0.18 at ~128 votes/pair; prevents cap-grinding on true ties. Replaced the old tiebreaker phase, removed in scheduler 1.4.0.)
4. **Cap** (`GAME_REPS=64` per variant) — absolute backstop.

**Minimum-length floor** (`GAME_MIN_QUERIES=6`): no stop rule may fire
before this many queries — every game pays 2 colds, so ending at 2-3 queries
is all-cold votes; the floor guarantees warm amortization (and sharper θ).

Overshoot: queries already in-flight when a rule fires still drain and count (their votes are valid BT data) — so a shutout may show 3 votes, not 2.

`ab`/`ba` splits escalate: slots are already warm → cheap reps under the gate.

## Rating terms

| Term | In code | Meaning |
|---|---|---|
| **θ (theta)** | `bt.get_theta(key)` | Bradley-Terry strength, fit by max-likelihood (Zermelo iteration) over ALL decisive votes, globally. Positive = above field average. Unbounded, not a percentage. Feed shows `θ→x (Δ)` = value after this game / movement caused by it. |
| **SE(gap)** | `bt.pairwise_gap_se()` | Fisher-covariance standard error of the θ *difference* between two images. Shrinks ~1/√(votes between them). The gate's currency. |
| **CI** | `bootstrap_ci` (dashboard, ~8s cache) | ◌ DISPLAY ONLY — ± band on θ. Overlap with the leader's CI = the "≈" mark = not statistically distinguishable from #1. The game gates use Fisher SE(gap), a different quantity. |
| **W / L** | `scheduler.states[k].wins/.losses` | ◌ DISPLAY ONLY — decisive **games** won/lost (scheduler counters). Nothing in the ranking engine reads them; θ comes from votes. A 128-vote war moves W by exactly +1; W/L ≠ vote counts. |
| **β (JUDGE_BETA)** | `config.JUDGE_BETA` | vestigial judge-noise display constant — ignore. |
| ~~σ / sigma~~ | nothing (OpenSkill era) | dead term; any remaining "sigma" label is a leftover. |

## Scheduler terms

| Term | In code | Meaning |
|---|---|---|
| **Phase** (per image) | `Phase` enum | ⚙ Lifecycle (v1.4.0): `NEW → RR_ACTIVE → RR_DONE → GREEDY`. Games pick pairs by the neediest unfinished phase first. |
| **RR** | `RR_CYCLES=1` | ⚙ Round-robin: every pair plays ≥RR_CYCLES games before greedy (drives pair selection). |
| **Tiebreaker** | — | REMOVED in scheduler 1.4.0 (user decision). All-tie pairs are handled by the SE-floor game rule instead. |
| **ε-greedy** | `EPSILON=0.10`, `_pick_greedy` | Pair selection once everyone is GREEDY: 90% most-informative pair (info-max scoring), 10% random. Re-picks murky pairs (revisits land in the cheap restore tier). |
| **Pivot** | `/pivot` endpoints | Manual override: force matches involving selected images (RR or greedy flavor). |
| **Hold 📌** | `holds.json`, `/revive`, `/unhold` | User pin on an image (revive flow). Marker only since v3 — nothing auto-eliminates. |
| **Gate / seed counter** | `_seed_counter` | Sequential seeds 1,2,3,...; resumes from the log's total query count on restart so `(prompt, seed)` tuples never re-fire. |

## Dashboard / feed symbols

| Symbol | Meaning |
|---|---|
| `#284` | game number |
| `56% ▶` / `◀ 44%` | pre-game win probability of the FIRST-listed image; arrow points at the favorite |
| `θ→+0.32 (+0.005)` | θ after the game (Δ = this game's movement) |
| `· 37g` (feed) | decisive **games** that image has played |
| "Votes" table column | decisive **votes** involving the image (query granularity — a 128-vote war adds 128) |
| `Votes ABA 0–3` | raw judge letters, then [first, second] vote counts; chips are colored by credited image (blue=left, purple=right) |
| `≈` next to θ | CI overlaps the leader — statistically tied with #1 |
| `Δsig` column | ◌ display — gap to the rank above is significant (1.645×Fisher SE) |
| RR column | ⚙ engine — pair-coverage progress toward RR_CYCLES × (field−1) games |
| Phase badges | ⚙ engine — per-image scheduler phase (see above); sidebar "Phase" = current selection policy label |
| ⚙ / ◌ tags (dashboard 2.5.0) | column header markers: ⚙ = read by the ranking engine, ◌ = display only — hover any header for specifics |
