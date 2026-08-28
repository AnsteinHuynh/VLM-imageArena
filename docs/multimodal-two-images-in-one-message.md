# Multimodal: two images in one message (llama-server + Qwen3.6-VL)

**Date:** 2026-08-14
**Project:** VLM-imageArena
**Author:** Neru (assistant)
**Status:** Findings complete; decision leaning toward *keep Jinja + one message with a text separator*.

---

## TL;DR

- **mmproj is loaded and working** — the vision encoder is fine.
- Under the **default Jinja chat template**, two images placed **adjacent** in a single user message cause the **second image to be silently dropped**. The model literally only sees the first image (verified by a red/blue perception probe).
- The drop is **not** the vision encoder, and **not** the Jinja *loop* (the template emits 2 media markers). It is the **media-marker substitution**: two adjacent markers collapse.
- Two working workarounds under Jinja:
  1. Each image in its **own user message** (the current production pattern in `judge.py`).
  2. Both images in **one message, separated by a text token** (`text` between the two `image_url` items).
- **`--no-jinja`** eliminates the adjacency drop entirely, **but** changes the reply format to `<think>…</think>…<letter>`, which breaks `judge._parse_vote` (it reads `content.strip()[0]` → `<`). Not a drop-in.

---

## Environment

| Item | Value |
|---|---|
| Model | `Qwen_Qwen3.6-35B-A3B-IQ4_XS-bart.gguf` (18.35 GB) |
| mmproj | `mmproj-Qwen_Qwen3.6-35B-A3B-bf16-bartowski.gguf` |
| llama.cpp build | `b10213-a09d8abf8` |
| Server | `--ctx-size 12000 --n-gpu-layers 99 --parallel 3 --reasoning off --mtmd-batch-max-tokens 2048 --flash-attn on --no-warmup` |
| Port | 9932 |
| `config.py` version | 2.0.2 |
| `media_marker` (from `/props`) | `<__media_Zxp2bThvzrB9AuwAy4p4jCQYLNbHzYGe__>` |

`/props` confirms the vision modality is active: `modalities: {vision: true, video: true, audio: false}`.

---

## Test methodology

All probes used solid-color PNG images (red = first image, blue = second image) and the question *"What color is the SECOND image?"*. Correct answer = **blue**. If the second image is dropped, the model answers **red** (only sees the first).

Probe scripts live in `<local probe dir>/20260814-llama-two-image-probe/`:

| File | What it tests |
|---|---|
| `probe_two_images.py` | Chat completions, separate vs one-message (512×512) |
| `probe_template.py` | `POST /apply-template` — dump rendered prompt + marker counts |
| `probe_matrix.py` | Chat completions, 4-case adjacency matrix (256×256) |
| `probe_completion.py` | `POST /completion` with the server's random marker |
| `probe_completion2.py` | `POST /completion` with the default `<__media__>` marker |

---

## Findings

### 1. mmproj is loaded (no problem here)

Server startup log: `loaded multimodal model, '…/mmproj-Qwen_Qwen3.6-35B-A3B-bf16-bartowski.gguf'`. Vision modality active. The bug is **not** a missing/incorrect mmproj.

### 2. Default Jinja — adjacency is what breaks it

`probe_matrix.py` (256×256, temp 0.0):

| Case | prompt_tokens | reply |
|---|---|---|
| 1. control — separate messages | 172 | `Blue` ✅ |
| 2. one message, **images adjacent** | 92 | `red` ❌ |
| 3. one message, **text between** | 165 | `Blue` ✅ |
| 4. one message, text first, images adjacent | 92 | `red` ❌ |

- Case 2 drops the second image (92 tokens ≈ one image; answer "red").
- Case 3 — the *only* difference from case 2 is a text token between the two `image_url` items — **works** (165 tokens ≈ two images; answer "Blue").
- Case 4 proves position doesn't matter: text *before* the pair does not help; it is specifically the **adjacency of the two image markers** that matters.

This also reproduces at 512×512 (`probe_two_images.py`: separate = 570 tokens/"Blue", one-message-adjacent = 290 tokens/"Red").

### 3. The Jinja template emits 2 markers (template loop is fine)

`probe_template.py` (`POST /apply-template`) for the one-message case:

```
<|im_start|>user
<__media_Zxp2bThvzrB9AuwAy4p4jCQYLNbHzYGe__><__media_Zxp2bThvzrB9AuwAy4p4jCQYLNbHzYGe__>What color is the SECOND image?<|im_end|>
```

Two media markers are emitted — **and they are identical strings**. The template's `render_content` loop correctly iterates every item in the content array. The problem is downstream of templating.

### 4. Raw `/completion` path (no template) shows the same adjacency failure

`probe_completion2.py`, using the default marker `<__media__>` + `multimodal_data`:

| Prompt | Result |
|---|---|
| `<__media__> <__media__> …` (space-separated) | `"The second image is **blue**"` ✅ |
| `<__media__><__media__> …` (adjacent) | empty output ❌ (tokens_predicted=2) |

This is the raw, template-free path (equivalent to `--no-jinja` for prompt construction) — yet adjacent markers still fail. This isolates the collapse to **llama.cpp's core media-marker substitution**, not the Jinja engine.

### 5. `--no-jinja` flag — fixes adjacency, but changes output format

Restarting with `--no-jinja` and re-running `probe_matrix.py`:

| Case | prompt_tokens | reply |
|---|---|---|
| 1. control — separate messages | 170 | `<think>\n\n</think>\n\nBlue` ✅ |
| 2. one message, **images adjacent** | 156 | `<think>\n\n</think>\n\nBlue` ✅ |
| 3. one message, text between | 164 | `<think>\n\n</think>\n\nblue` ✅ |
| 4. one message, text first, images adjacent | 156 | `<think>\n\n</think>\n\nBlue` ✅ |

Every case now sees both images (case 2 jumped from 92 → 156 tokens and answered correctly). `--no-jinja` uses the "Content-only" fallback format, which inserts distinct media markers per image, so adjacency is safe.

**However**, the reply now carries a `<think>…</think>` wrapper. `judge._parse_vote` reads `content.strip()[0]`, which would now be `<`, so **every vote parses as malformed**. `--no-jinja` is therefore not a drop-in fix without parser changes (or re-enabling `JUDGE_USE_GRAMMAR`, or a custom prompt that suppresses the think block).

---

## Root cause (leading hypothesis)

In the **Jinja chat-completions path**, when multiple images appear in a single message, the server substitutes them with a **non-unique media marker** (the `/apply-template` dump shows identical strings for both images). The downstream marker→vision-token substitution keys on the marker string, so two identical **adjacent** markers collapse into a single image. A text token between them breaks the adjacency and avoids the collapse.

The `--no-jinja` "Content-only" path assigns distinct markers per image and is unaffected — but introduces the think-wrapper output regression.

> This is an empirical characterization from probes; the exact C++ mechanism in `llama.cpp` was not traced to source. The behavior is stable and reproducible across 5 independent probes.

---

## Implications for VLM-imageArena

- **Current pattern (separate messages) already works** — nothing is broken today.
- **The project's earlier "two images in one message" rule was accurate but incomplete**: it is specifically *adjacent* images that drop; a text separator in the same message already works under Jinja.
- If we move to **one message + text separator** (keep Jinja), `judge.py` would change `query()` to send both images in a single user message with a `text` item between them. This keeps the clean `A`/`B` reply format (no think wrapper), keeps `_parse_vote` unchanged, and lets the judge see both images side-by-side in one prompt.

---

## Decision (2026-08-14)

**Leaning: keep Jinja + "one message, text between".** Not yet implemented in code — this document records the finding and the decision direction; `judge.py` still uses separate messages until the change is explicitly made.

---

## Reproduction

```powershell
# 1. start the judge server (default Jinja config)
cd <repo>
python -m src.judge_server

# 2. run the adjacency matrix
cd <local probe dir>/20260814-llama-two-image-probe
python -u probe_matrix.py
```

To test `--no-jinja`, launch `llama-server.exe` directly with `--no-jinja` appended (see `judge_server._build_cmd()` for the base command), then re-run `probe_matrix.py`.
