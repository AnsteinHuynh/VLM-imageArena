# History: OpenSkill-era architecture log (HSP)

> **Archived research log, 2026-08-04..06.** Kept for provenance: it documents
> the ORIGINAL OpenSkill-based arena (blindness bug, order-bias cancellation,
> the 4-slot sweet spot) that preceded the current Bradley-Terry + pinned-slot
> game protocol. The architecture it describes is SUPERSEDED -- read
> [game-protocol-pinned-slots.md](game-protocol-pinned-slots.md) and
> [glossary.md](glossary.md) for the current system. Local paths are
> placeholder-ized.
# VLM-imageArena — Architectural Overview & Handoff (HSP)
**Date:** 2026-08-04 (evening session, ~21:16–23:50 CDT)
**Status:** WORKING, verified end-to-end. Ranking pipeline fixed after a critical blindness bug.

---

## 1. What this program is

An **adaptive A/B image-ranking arena**. Given N candidate images in `in/`, it uses a
single VLM judge (Qwen3.6-35B-A3B via llama-server) to compare pairs ("which is
better?"), and builds a statistically-grounded ranking using the **OpenSkill**
rating system (Bayesian, mu = rating, sigma = uncertainty).

Key design properties:
- **Adaptive sampling**: pairs with similar ratings AND high uncertainty get tested
  more (score = `predict_draw(a,b)` from openskill). Converged pairs stop being tested.
- **Counterbalanced matches**: each verdict = `QUERIES_PER_MATCH` (4) judge queries,
  half with image X first, half with Y first, majority vote, ONE rating update.
  This mathematically cancels the judge's order preference (see history §7.3/§7.4).
- **Crash-safe**: every match is appended to `out/battles.jsonl` immediately;
  restart resumes by replaying the log (deterministic openskill replay).
- **Auditable**: every query's prompt, seed, image order, raw response, and vote
  are recorded.

## 2. Current status (verified 2026-08-04 evening)

- ✅ Judge sees BOTH images (was blind to image 2 — fixed, see §7.4)
- ✅ Thinking mode off (`--reasoning off`), clean single-char A/B votes
- ✅ Query-level order bias ≈ 48.4% (statistically fair) after the fix
- ✅ PNG lossless pipeline (was JPEG; webp data-URIs are DROPPED by this server build)
- ✅ 4 slots × 3000 ctx = measured sweet spot (0.65s/query era; PNG+2img ≈ 1.0-1.15s)
- ✅ `out/` contains a REAL run from the fixed pipeline: 693 matches (475 decided + 218 ties) from ~23:11–23:46
- ⚠️ The 905-game log from earlier tonight is INVALID (blind-judged) — that data
  was in `out/` and was overwritten by the fresh run (user ran `--fresh` or the
  new log replaced it). Do not mix old logs into analysis.
- ⚠️ 31% tie rate in the current log (temp 1.0 + 4-query matches) — see §9 open questions

## 3. How to run

```bat
:: 1) Start the judge server (visible console, Ctrl+C to stop):
start_judge.bat

:: 2) Run the arena (auto-resumes; --fresh wipes out/):
run.bat                 :: or: run.bat --fresh --max-queries 500

:: 3) Any time: regenerate ranking.csv from the log without the server:
standings.bat
```

Manual equivalents (venv1 python, full paths):
```
python -m src.arena --dry-run
python -m src.arena --fresh
python -m src.standings
```

## 4. Architecture

```
in/ (candidate images)            src/config.py (ALL tunables, v1.8.3)
      │
      ▼
src/images.py (v1.0.0)            discover + Lanczos resize to MP band + PNG encode
      │  prepared bytes (cached per image)
      ▼
src/scheduler.py (v1.1.0)         openskill: new_rating(), choose_pair(ratings, blocked),
                                  apply_result(ratings, stats, winner, loser)
      │  pair (first, second), coin-flipped order
      ▼
src/judge.py (v1.2.1)             HTTP client for llama-server /v1/chat/completions.
                                  CRITICAL: each image in its OWN user message.
                                  Strict vote parse: first char must be A/B followed by
                                  non-letter ("A", "A.", "A because..." all valid;
                                  "AB"/"Alpha" malformed). Returns (letter, full_raw).
      │  letter per query
      ▼
src/arena.py (v1.3.0)             orchestrator: ThreadPoolExecutor (ARENA_CONCURRENCY
                                  workers, default 4) + threading.Lock. Per match:
                                  4 queries (2 per order, shuffled), majority = verdict,
                                  ties logged as winner=null (no rating change).
                                  Reserved-set prevents duplicate in-flight pairs.
                                  In-memory skip counts; pair banned after 5 malformed.
                                  Checkpoint: append battles.jsonl per match.
      │
      ▼
src/standings.py (v1.0.1)         replay battles.jsonl -> ratings -> ranking.csv.
                                  Shared replay/CSV logic imported by arena.py.
                                  Ties (winner=null) skipped. CLI: --in/--out.

src/judge_server.py (v1.0.3)      launcher for llama-server (start_judge.bat):
                                  builds cmd from JUDGE_* config, health-waits,
                                  keeps console attached. NOTE: --cache-reuse was
                                  REMOVED (no-op for multimodal; config knob is dead).

src/prompts.json                  ~15 judge question phrasings (editable).
```

Data flow per match: `choose_pair` → 4× `judge.query` (alternating order) →
majority → `apply_result` → `battles.jsonl` append → CSV refresh every 50.

## 5. Data formats

### out/battles.jsonl (one JSON line per MATCH)
```json
{
  "game": 475,            // decided-match counter (ties have "game": null)
  "pair": [pathA, pathB], // sorted
  "first": pathA, "second": pathB,  // canonical order at pick time
  "winner": pathA,        // null for ties
  "tie": false,
  "queries": [            // 4 entries (or fewer if network errors mid-match)
    {"order": [pathB, pathA], "letter": "A", "raw": "A",
     "prompt": "Which picture is better?", "seed": 12345}
  ],
  "ts": 1785900402.96
}
```
Old v1 lines (single-query games) lack `queries`/`tie` — replay still handles them.

### out/skips.jsonl — malformed votes: {pair, seed, prompt, raw, ts}
### out/ranking.csv — rank,file,mu,sigma,wins,losses,games,winrate (sorted by mu)

## 6. Config highlights (src/config.py v1.8.3 — full current values)

- **Paths**: IMAGE_IN_DIR/OUT_DIR/PROMPTS_FILE (absolute)
- **Judge**: SERVER_URL :9932/v1/chat/completions; SYSTEM_PROMPT (user-edited,
  includes "Don't respond with 'Sure! I can do that...'" — keep the strict A/B contract);
  TEMPERATURE=1.0, TOP_P=0.95, TOP_K=20, MIN_P=0.0, PRESENCE_PENALTY=1.5,
  REPETITION_PENALTY=1.0 (HF general-task preset, thinking off); MAX_TOKENS=10
- **Server launch**: JUDGE_SERVER_EXE (llama-gpu build), JUDGE_MODEL
  (<models>/LLAMA-Models/qwen36-35b/Qwen_Qwen3.6-35B-A3B-IQ4_NL-bart.gguf),
  JUDGE_MMPROJ (bf16 bartowski), JUDGE_CONTEXT=12000, JUDGE_NGL=42,
  JUDGE_SPEC_TYPE="draft-mtp", JUDGE_REASONING="off", JUDGE_PORT=9932,
  JUDGE_PARALLEL=4 (measured sweet spot)
- **Images**: MIN_MEGAPIXELS=0.4, MAX_MEGAPIXELS=1.5 (USER PLANS TO CHANGE TO ~1.2MP
  EVERYTHING — see §9), ENCODE_FORMAT="PNG"
- **Rating**: INITIAL_MU=25, INITIAL_SIGMA=25/3, TARGET_SIGMA=3.0,
  QUERIES_PER_MATCH=4, MAX_QUERIES=20000, SIGMA_PREFILTER=20, TOP_K_CANDIDATES=10,
  MAX_MALFORMED_PER_PAIR=5, ARENA_CONCURRENCY=4, CSV_REFRESH_EVERY=50

## 7. Critical history — bugs found & fixed (the gotchas)

### 7.1 openskill v6.2.0 is a NEW class-based API
`from openskill.models.weng_lin import bradley_terry_full as bt; model = bt.BradleyTerryFull(mu, sigma)`;
`model.rating()`, `model.rate([[a],[b]], ranks=[0,1])`, `model.predict_draw([[a],[b]])`.
ALL online examples show the old v4/v5 API (`from openskill import Rating, rate`) —
does NOT exist in 6.2.0. Non-mutating: rate() deep-copies; returns new Rating objects.

### 7.2 Qwen3.6 thinks by default → empty content
With `max_tokens=8` and thinking ON, the model burns the whole budget on
`reasoning_content` → `content: ""` → 100% skip rate. FIX: server flag
`--reasoning off` (config JUDGE_REASONING). Verification: response JSON has
`reasoning_content: None` and content is a clean letter.

### 7.3 The blind judge — THE critical bug
**Symptom**: 75.6% first-position win rate; random-walk rankings; duplicate pair
77% first-wins. **Root cause**: with BOTH images + question in ONE user message
content array, this llama.cpp build + Qwen3.6-VL silently DROPS the second image.
Prompt eval token counts proved it: old queries ~1070 tokens = ONE image (1039)
+ text; a 2-image query should be ~2100. The model literally answered "There is
no second image to describe." **Fix**: each image in its OWN user message
(judge.py v1.2.0). Verified: model describes both images, lists differences;
query-level order bias dropped to 48.4%. **The entire pre-fix ranking data is
invalid.**

### 7.4 Order bias after the fix → counterbalanced matches
After separate-message fix, a 33-game sample suggested ~70% SECOND-image
preference (recency). Larger sample (62 votes): 48.4% — mostly noise, but the
counterbalanced match design (QUERIES_PER_MATCH=4, 2+2 orders) cancels ANY order
bias mathematically. Ties (2-2) log winner=null, no rating change — identical
images naturally end in ties (the duplicate pair can't win).

### 7.5 webp data-URIs are dropped by this llama-server build
Raw `data:image/webp;base64` → model says "I cannot see the images". JPEG and
PNG both work. Pipeline uses PNG now (lossless, config ENCODE_FORMAT).

### 7.6 Slots / context / VRAM measurements (4090 24GB, ngl 42)
- 1 slot: ~1.0s/query; 4 slots: 0.65s/query (best); 5 slots: 0.66 (no gain);
  8 slots: 0.97 (VRAM exhausted: 247MB free → spills to CPU). 4 slots × 3000 ctx.
- Context is PARTITIONED per slot (kv_unified=false): 12000/4 = 3000/slot
  (rounded to 3072). 2-image prompts at 1.04MP ≈ 2100 tokens; max ~1.5MP per
  image before slot overflow (1920² pair failed: context overflow).
- RAM: llama-server settles ~35GB private under load (model 20GB + 22 CPU layers
  + mmproj + KV + slot buffers); flat after ~30s (no leak); only a restart
  returns to ~25GB. Arena client ~100MB, clean exit.

### 7.7 llama.cpp flag renames / PowerShell gotchas
- `--draft-mtp` → `--spec-type draft-mtp` (current builds)
- PowerShell won't run bare `llama-server` from cwd — use full path or `.\`
- `--cache-reuse` is a NO-OP for multimodal ("cache_reuse is not supported by
  multimodal, it will be disabled") — removed from launcher (config knob dead)
- BUT the per-slot PROMPT cache works with images (same bytes + same position
  + same slot -> vision encode + LLM re-eval skipped). arena v1.4.0 exploits
  it: grouped orders + id_slot pinning (see §13).
- `--image-min-tokens 1024` warning (grounding tasks) is NOT needed for
  aesthetic A/B judging — 500-1000 tokens/image is fine for "which is better"

### 7.8 Operational
- Mock server kill silently failed once → zombie mock held port 9932 → smoke test
  hit the mock. Always verify port owner after kills (`Get-NetTCPConnection`).
- run.bat had a bogus `--resume` flag (resume is automatic) — removed.
- First arena refactor to standings.py left a duplicate `def _load_prompts():`
  (empty def) → IndentationError. Fixed; import-check before commit.

## 8. Verified measurements (for reference)

- **Resolution→tokens curve** (live server): 256²→53 tok, 512²→245, 720²→518,
  944×1104 (1.04MP)→1039, 1024²→1013, 1280²→1589, 1600²→2489. ~1000 tokens/MP,
  linear. NO fixed "native" resolution in preprocessor config (patch 16, merge 2,
  shortest_edge 256px, longest_edge 16MP). Family reference ≈ 1MP.
- **Performance**: ~0.65s/query (JPEG era, 1-slot-era numbers differ); with PNG +
  2 images ≈ 1.0-1.15s/query; match ≈ 4.5s.
- **Bias**: 75.6% first-wins (blind, broken) → 48.4% (fixed, fair).
- **Memory**: see §7.6.
- **Current log** (out/, fixed pipeline, 23:11-23:46): 693 matches = 475 decided
  + 218 ties (31%). Ranking leader 1785871834 (mu 32.4); PureWhite crushed to
  mu -1.5 (0-28 — the judge clearly sees it as worst; real signal).

## 9. Open questions / tomorrow's plan (user's stated direction)

1. ~~Normalize ALL images to ~1.2MP~~ DONE (2026-08-05): MIN=MAX=1.2 uniform.
2. ~~Try IQ4_XS quant~~ DONE (2026-08-05): IQ4_XS + ctx 40000 + ngl 42 requested
   by user; VRAM fit unverified yet. NOTE: changing the model file invalidates
   prior ratings only if the judge's opinions change materially; the JSONL
   replay stays deterministic but the JUDGE's taste may shift → consider --fresh.
3. **Fiddle with server flags** (slots/context/ngl trade-offs on the 4090).
4. **31% tie rate**: temp 1.0 makes the judge noisy → many 2-2 matches. Options:
   QUERIES_PER_MATCH=6 (3+3), lower temp (0.7), or accept (ties are honest).
5. **Duplicate pair** (2095 vs Copy, byte-identical) currently #4 vs #7 — still
   within sigma of each other; watch whether they converge to equal with more games.
6. **Per-prompt bias pruning**: earlier (blind era) "Which image would you choose?"
   was worst (94.8% biased); "Between these two pictures" least (67%). Re-measure
   per-prompt bias in the FIXED era before pruning prompts.json.
7. **Convergence speed**: TARGET_SIGMA=3.0 declared "converged" while mu still
   random-walks ±0.2/game at sigma 1.6 (measured). Consider TARGET_SIGMA=1.5-2.0
   and/or MIN_GAMES per image for honest convergence.
8. Consider `--image-min-tokens`/`--image-max-tokens` only if spatial/grounding
   tasks ever matter (they don't for aesthetics).
9. The 4090 is the binding constraint (24GB VRAM, 1GB free at ngl 42). RAM is
   fine (48GB, ~6GB free under load).

## 10. Files & versions (git repo initialized in project folder)

| File | Version | Notes |
|---|---|---|
| src/config.py | 1.9.0 | IQ4_XS, ctx 40000, MTP off, ubatch/mtmd knobs, 1.2MP uniform |
| src/judge.py | 1.3.0 | + id_slot pinning, tokens_cached audit field |
| src/arena.py | 1.4.0 | Per-slot match loops, grouped orders, early exit |
| src/scheduler.py | 1.1.0 | openskill wrapper (v6 API), predict_draw scoring, blocked sets |
| src/standings.py | 1.0.1 | Replay→CSV; ties skipped; shared with arena |
| src/images.py | 1.0.1 | + band-inversion guard |
| src/judge_server.py | 1.1.0 | + --ubatch-size / --mtmd-batch-max-tokens |
| src/slotcalc.py | 1.0.0 | NEW: MP/ctx -> slots-fit calculator |
| src/prompts.json | — | 15 phrasings (editable) |
| run.bat / start_judge.bat / standings.bat | — | CRLF+ASCII, thin wrappers only |
| debug-image-perception/ | — | probe_vision.py (v2, PNG+swap+dup), probe_formats.py (A/B/C/D formats), test_red.png/blue.png |

Git log: 20+ commits, latest `50e1dc3 config 1.8.3 + judge 1.2.1: HF general-task sampling preset`.

## 11. Environment

- Windows 11, 4090 24GB, 48GB RAM (RTX 4090, Ryzen 7 5800x3D)
- Python: `python` (openskill 6.2.0,
  pillow 12.3.0, requests)
- llama-server: `llama-server.exe`
  (CUDA build; model IQ4_NL 35B-A3B + bf16 mmproj, ngl 42, MTP draft, port 9932)
- Test infrastructure: `<local test dir>/`
  (mock_server.py — ThreadingHTTPServer, asserts temp 1.0/top_p 0.95; bench.py,
  bench2.py, bias.py, audit.py, history.py, pixdiff.py, res_curve.py, sample images)
- in/ currently holds 8 images: 6 original xlnegshuf webp (incl. byte-identical
  Copy pair), 1 newer 1785880864, 1 PureWhite-200.png control

## 12. Gotchas recorded (operational pitfalls)

1. openskill v6 = class API; old imports fail. (7.1)
2. Qwen3.6 thinking ON by default → empty content; `--reasoning off` required. (7.2)
3. Two images in ONE message → second image silently dropped (llama.cpp/Qwen3.6-VL). Separate messages mandatory. (7.3)
4. webp data-URIs dropped by server; use PNG/JPEG. (7.5)
5. 4 slots is the 4090 sweet spot; 8 slots backfires (VRAM). (7.6)
6. Context partitions per slot (kv_unified=false); 2×1.5MP is the practical ceiling. (7.6)
7. `--draft-mtp` renamed to `--spec-type draft-mtp`. (7.7)
8. `--cache-reuse` no-op for multimodal. (7.7)
9. Mock-server kill can silently fail — verify port owner. (7.8)
10. Old-format ranking data (pre separate-message fix) is INVALID — never mix. (7.3)
11. The user edits config.py SYSTEM_PROMPT directly — respect their wording.
12. Ties (2-2) are common (~31%) with temp 1.0 — by design, not a bug.
13. Concurrent 4-query matches (pre-1.4) get ZERO prompt-cache reuse — grouped
    orders + id_slot pinning required to exploit it. (13)
14. tokens_cached in the response is the audit proof of reuse — logged per query. (13)
15. 40k ctx at ngl 42 may not fit 24GB VRAM — check at server start. (13)
16. One arena run shows as TWO python.exe processes (Windows venv launcher +
    child interpreter). 2026-08-06: misdiagnosed as duplicate arenas for
    hours — the "261 racing pairs" were actually tie-retries. Check
    parentage, not counts. (memory §7)

## 13. Speedup session 2026-08-05 (user-approved) — match protocol v2

### Research verdict (2 subagents)
- NO image-embedding cache exists anywhere (llama.cpp, oobabooga/textgen,
  webtextgen/VLM-GGUF flag list). The only working mechanism is llama.cpp's
  per-slot PROMPT cache: same image bytes (FNV-1a hash) + same prompt position
  + same slot -> vision-tower encode AND LLM re-eval for those tokens skipped.
  Host prompt cache (--cache-ram) also stores vision output but keys on the
  ENTIRE prompt (identical text) -> useless with varied phrasings.
- Future lead: llama.cpp issue #25854 / PR #26640 (2026-08-05) — slot
  save/restore with images; unmerged.

### Config (v1.9.0)
- Model: IQ4_XS (18.35GB, was IQ4_NL 19.33GB). ctx 40000 (user-requested),
  ngl 42. WARNING: KV at 40k ≈ 5.3GB fp16 vs 1.6GB at 12k — fit on 24GB is
  questionable (IQ4_XS recovers ~1GB). If it spills/OOMs: fewer slots first,
  then lower ngl, then ctx 24k.
- MTP dropped (--spec-type none): output is ~10 tokens, generation help is
  worthless; draft weights wasted VRAM.
- --ubatch-size 1024, --mtmd-batch-max-tokens 2048.
- Images: MIN=MAX=1.2MP uniform (Lanczos; equal pixels = equal tokens = no
  size bias). Band logic still works for MIN < MAX; inversion guarded.
- image-max-tokens stays None (uniform client resize makes the clamp moot).

### Match protocol v2 (arena v1.4.0) — Option A
- Each worker pinned to its own server slot (id_slot = worker index; workers
  must be <= JUDGE_PARALLEL).
- Queries GROUPED by order: [order1, order1, order2, order2] (order1 coin-
  flipped, order2 = reverse). q2 reuses q1's ~2100-token image prefix,
  q4 reuses q3's (~0.15s vs ~1.1s) — proven via tokens_cached in the log.
- EARLY EXIT: same-order 2-0 after q1,q2 = decided (avg ~3.08 queries/match
  vs 4 → ~2.4× faster expected: ~1.8s/match vs 4.4s).
- Caveat (documented): a same-order 2-0 doesn't cancel position bias within
  that pair; measured bias is 48.4% (noise) → acceptable. Full-4 matches use
  both orders 2× (counterbalancing intact).
- battles.jsonl: matches gain "early": bool; queries gain "tokens_cached".
  Old log lines replay unchanged (additive fields).

### Benchmarks still queued
- 4 vs 5 slots at 40k ctx (5 → 8192/slot, fits 2450 easily; VRAM is the
  question, not tokens).
- Verify tokens_cached > 0 on q2/q4 (reuse actually hitting).
- If judge taste shifted with IQ4_XS: consider --fresh (user's call).

## 14. Judge-mode benchmark session (2026-08-05/06) — thinking ON verdict

Full record: `<memory note> 2026-08-06-VLM-imageArena-Logo-Benchmarks`

- **Thinking ON = pipe dream for this arena.** At budget 128 it carries a
  first-image positional bias (flips OFF's B-votes→A 10-14:1) and costs
  3× at best; at budget 2000 it's 18× slower. Against Anstein's position-free
  RATINGS (JO=5, BQ=5, 71=4, Gh/oh/pi=3) with the logo context prompt:
  OFF Spearman 0.514 (best), ON 0.214, always-A baseline -0.04.
- **Context prompt is load-bearing**: logos have white=TRANSPARENT backgrounds;
  without the context the judge grades white squares. Context text lives in
  config.py SYSTEM_PROMPT (config 1.9.1, appended by Shizuka, user may reword).
- ALL pre-context arena data is invalid for logo ranking (--fresh was run).
- Human ground truth: pair verdicts are position-confounded (85% A) and the
  user can't unsee images → use single-image RATINGS as ground truth.
- Overnight run: 170 images from selected2 + PureWhite control in in/,
  arena --fresh → src/championship.py → out/WINNER.md (DECISIVE/PROBABLE/
  INCONCLUSIVE via 10 best-of-4 H2H matches + ranking gap).
- MISDIAGNOSIS LESSON (2026-08-06): "duplicate arena processes" were venv
  python launcher + child interpreter — one run = two same-cmdline processes
  in the same second. The lock (arena 1.4.1, O_EXCL) is kept as a guard.

## 2026-08-06 — Multi-tournament + atomic CSV + championship top-N

- --tourney NAME (arena/standings/championship): in/{NAME}/ + out/{NAME}/;
  "default" = legacy in/ + out/ (no migration, old data untouched).
  Name regex [A-Za-z0-9_-]{1,64} (config.py 1.10.0).
- Lock stays GLOBAL at out/arena.lock (arena 1.5.0) — one tourney at a time
  (shared judge server; user decision).
- CSV crash bug FIXED: direct write to ranking.csv while open in Excel raised
  PermissionError inside a worker thread and killed the run. Now atomic
  tmp+rename (src/report.py 1.0.0): locked target → skip with warning, never
  crash, never half-write. Verified LIVE 2026-08-06: Excel held ranking.csv
  open; standings completed exit 0 with "[report] ... SKIPPED" while
  ranking.html still published.
- ranking.html viewer (30s auto-refresh, sortable, sigma color coding) +
  history/ranking-{N}.csv snapshots every 500 games (keep newest 5; numeric
  sort — lexicographic sort bug caught in testing).
- Championship (1.1.0) = ROUND ROBIN: top CHAMPIONSHIP_TOP_N (default 3) +
  CHAMPIONSHIP_EXTRA_IMAGES (absolute path list, may be unranked; missing
  paths warn+skip). CHAMPIONSHIP_MATCHES per pair (10). Verdict: DECISIVE if
  win gap >= 6 or mu gap >= 2 sigma; PROBABLE if >= 4; else INCONCLUSIVE.
  --dry-run prints contender set + schedule without querying.
- in/ UNTRACKED in git (user decision): .gitignore now in/* + !in/.gitkeep.
  Old images stay in git history. Old tourney results backup:
  <repo>-backup-20260806\out\
- Full test suite passed 2026-08-06: imports, standings regen (rankings
  byte-stable vs pre-change CSV), locked-CSV skip, snapshot prune, arena
  --dry-run (3-image scratch tourney), champ --dry-run (fake 3-game log),
  invalid tourney name, missing tourney dir, global lock lifecycle.
  Scratch fixtures: ai/test/20260806-arena-smoke + ai/test/20260806-reporttest.

## 2026-08-06 — Protocol BUGFIX (arena 1.5.1 / championship 1.1.1): fake 1-1 ties

- The early-exit check `votes[first]+votes[second] == 2` fired on 1-1 splits too
  -> q3/q4 (reverse-order counterbalancing) NEVER ran. Every match was
  same-order Bo2; "ties" were fake 2-query 1-1 exits (early=true). Present
  since protocol v2 (arena 1.4.0); the ENTIRE logo tourney ran this way:
  32,206/32,207 matches were 2-query, 44.3% were fake ties. The old "~31%
  ties by design" note was this bug.
- Position-bias re-measurement: B wins ~61-64% of queries in BOTH logs (logo
  B:39,353/A:25,062; pfp B:1,020/A:585). The old "48.4% noise" figure was an
  early small sample. With the bug, ~71% of decided matches went to the
  B-position image regardless of quality -> logo ranking degraded, pfp
  ranking substantially bias-driven.
- Fix: early exit only on a genuine 2-0. New config ALWAYS_FULL_MATCHES=True
  (default) plays all 4 queries per match -> per-match counterbalancing, bias
  cancels. Expect higher tie rates (honest 2-2) + slower convergence.
- pfp tourney: restart with fresh.bat after this fix (pre-fix matches are
  same-order Bo2 with uncancelled bias).
- Logic tests: 7-case matrix passed (full/fast mode x 2-0/1-1/0-0).

## 2026-08-06 — Speed pass: total-pixel budget (768x768) + flash attention

- Image convention changed from megapixels to TOTAL PIXELS (user call: MP is
  an "awful convention"): config MIN/MAX_TOTAL_PIXELS = 768*768 = 589,824 px
  (config 1.11.0, images 1.0.3). Tokens: ~1000/MP linear -> ~590 tokens per
  image, ~1229 per 2-image query (was ~2450 at 1.2MP) -> cold queries ~25-30%
  faster. slotcalc converted to --px (1.0.1).
- Flash attention ON: JUDGE_FLASH_ATTN=True -> judge_server 1.1.1 passes -fa.
  Server restart REQUIRED (KV cache format changes). Note: the old 8-slot
  collapse was measured at fp16 KV; -fa lowers KV VRAM so a slot re-sweep may
  behave differently now (queued work).
- sigma 3.0 kept; temp 1.0 kept (tie rate is 16.2% on the fixed protocol —
  no temp tuning needed).
- Arena dry-run now prints px (arena 1.5.2).
- Verified: 512x512 -> 768x768 = 589,824 px exact; 2048x2048 -> 768x768;
  1200x800 -> 941x627 (aspect preserved). slotcalc: 12 slots fit token-wise
  at 40k ctx.
