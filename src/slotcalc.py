"""
VLM-imageArena — slot calculator: how many judge-server slots fit a given
image size + context? Token-fit only, not VRAM.
Version: 1.0.1

Answers: "If images are X total pixels and the server context is Y, how many
slots fit?" Per-slot overhead is empirical — verify with a slot sweep.

Usage:
  python -m src.slotcalc --px 589824 --ctx 40000
  python -m src.slotcalc --px-range 262144,589824,1048576 --ctx-range 12288,24576,40000
"""

VERSION = "1.0.2"

import argparse

from . import config

TOKENS_PER_PIXEL = 0.001   # measured ~1000 tokens/MP, linear (Qwen3.6-VL family res_curve)
TEXT_OVERHEAD = 50         # system prompt + image labels + question (~50 tokens)


def tokens_per_query(px):
    """Estimated prompt tokens for one 2-image query at `px` total pixels each."""
    return int(2 * px * TOKENS_PER_PIXEL + TEXT_OVERHEAD)


def detail(px, ctx, max_slots=12):
    """Per-slot context (ctx//slots) vs needed tokens, for each slot count."""
    needed = tokens_per_query(px)
    out = []
    for slots in range(1, max_slots + 1):
        psc = ctx // slots
        out.append((slots, psc, needed, psc >= needed, psc - needed))
    return out


def max_slots(px, ctx, max_slots_count=12):
    """Largest slot count whose per-slot context still fits one query."""
    needed = tokens_per_query(px)
    best = 0
    for slots in range(1, max_slots_count + 1):
        if ctx // slots >= needed:
            best = slots
    return best


def fmt_detail(px, ctx, max_slots_count=12):
    needed = tokens_per_query(px)
    print(f"Query tokens (2x {px} px images): ~{needed}")
    print(f"Context: {ctx} | per-slot context at N slots:")
    for slots, psc, n, fits, head in detail(px, ctx, max_slots_count):
        mark = "OK " if fits else "NO "
        print(f"  {slots:2d} slots: {psc:>7} tokens/slot  {mark} (headroom {head:+d})")
    best = max_slots(px, ctx, max_slots_count)
    print(f"\nMax slots that fit: {best}" if best
          else "\nNo slot count fits — raise ctx or lower image pixels.")


def fmt_grid(px_range, ctx_range):
    print("Max slots that fit (rows = total pixels, cols = ctx):")
    print("PX      " + "".join(f"{c:>9}" for c in ctx_range))
    for px in px_range:
        row = f"{px:<7}" + "".join(f"{max_slots(px, c):>9}" for c in ctx_range)
        print(row)


def main():
    p = argparse.ArgumentParser(
        description="VLM-imageArena — slot-fit calculator (token-fit only)")
    p.add_argument("--px", type=int, default=config.MAX_TOTAL_PIXELS,
                   help="total pixels per image (default: config MAX_TOTAL_PIXELS)")
    p.add_argument("--ctx", type=int, default=config.LLAMA_ARG_CTX_SIZE,
                   help="server context (default: config LLAMA_ARG_CTX_SIZE)")
    p.add_argument("--px-range", type=str, default=None,
                   help="comma list of total-pixel values for the grid view")
    p.add_argument("--ctx-range", type=str, default=None,
                   help="comma list of context sizes for the grid view")
    p.add_argument("--max-slots", type=int, default=12,
                   help="slot ceiling to consider (default 12)")
    args = p.parse_args()
    if args.px_range or args.ctx_range:
        px_r = [int(x) for x in (args.px_range or "589824").split(",")]
        ctx_r = [int(x) for x in (args.ctx_range or str(config.LLAMA_ARG_CTX_SIZE)).split(",")]
        fmt_grid(px_r, ctx_r)
    else:
        fmt_detail(args.px, args.ctx, args.max_slots)


if __name__ == "__main__":
    main()
