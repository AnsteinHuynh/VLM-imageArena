"""
VLM-imageArena — manual judge A/B tester.
Version: 1.0.0

Fires the EXACT arena query path (images.prepare_data_url normalization ->
config.SYSTEM_PROMPT -> separate image messages -> fixed sampling params ->
judge.query) against the running judge server, so you can sanity-check a new
judge model before an arena run.

Usage (from repo root, or via manual_test.bat):
  python -m src.manual_test IMG1 IMG2 [--question "..."] [--seeds N] [--swap] [--slot N]

  --seeds N   run N queries with sequential seeds 1..N (default 1) — shows vote
              variance under the arena's temperature=1.0 sampling.
  --swap      ALSO run each query with image order reversed (A<->B) — exposes
              position bias on this judge (old Qwen3.6 measured gamma=+0.436).
  --slot N    pin all queries to server slot N (default 0) — pinned repeats
              hit the prompt cache; watch tokens_cached grow after query 1.

Output: per-query line (seed, order, vote, raw text, latency ms, cached
tokens) + a summary tally. Exit code 0 = server answered all queries;
2 = server not healthy (start start_judge.bat first); 1 = other error.
"""

VERSION = "1.0.0"

import argparse
import sys
import time

from . import config, images, judge

DEFAULT_QUESTION = (
    "Which image looks better overall? Answer with exactly one character: A or B."
)


def main():
    ap = argparse.ArgumentParser(
        prog="src.manual_test",
        description="Manually test the VLM judge with two images (arena-identical query).",
    )
    ap.add_argument("img1", help="path to image A (first shown to the judge)")
    ap.add_argument("img2", help="path to image B (second shown to the judge)")
    ap.add_argument("--question", default=None,
                    help="judge question (default: generic better-image question; "
                         "arena runs use prompts.json[0] via FIXED_QUESTION)")
    ap.add_argument("--seeds", type=int, default=1, metavar="N",
                    help="run N queries with sequential seeds 1..N (default 1)")
    ap.add_argument("--swap", action="store_true",
                    help="also run every query with A/B order reversed (position-bias check)")
    ap.add_argument("--slot", type=int, default=0, metavar="N",
                    help="server slot to pin (default 0)")
    args = ap.parse_args()

    print(f"[manual_test] v{VERSION} — judge {config.MODEL_NAME} at {config.SERVER_URL}")
    if not judge.server_healthy():
        print(f"[manual_test] ERROR: judge server not healthy at {config.SERVER_HEALTH_URL}")
        print("[manual_test] start it first: double-click start_judge.bat")
        return 2

    # Question: default to the arena's fixed question (prompts.json[0]) when
    # present, else the generic one. --question overrides both.
    question = args.question
    if question is None:
        try:
            import json
            prompts = json.loads(open(config.PROMPTS_FILE, encoding="utf-8").read())
            question = prompts[0] if prompts else DEFAULT_QUESTION
        except (OSError, ValueError, IndexError):
            question = DEFAULT_QUESTION
    print(f"[manual_test] question: {question!r}")
    print(f"[manual_test] normalizing images to "
          f"{config.MIN_TOTAL_PIXELS}..{config.MAX_TOTAL_PIXELS} px band "
          f"({config.ENCODE_FORMAT}) — same as arena")

    t0 = time.time()
    url1 = images.prepare_data_url(args.img1)
    url2 = images.prepare_data_url(args.img2)
    print(f"[manual_test] prep done in {time.time()-t0:.1f}s "
          f"(disk-mirror cache may make repeats instant)")

    orders = [("A,B", url1, url2)]
    if args.swap:
        orders.append(("B,A (swapped)", url2, url1))

    tally = {}  # (order_label) -> {"A": n, "B": n, "malformed": n}
    for order_label, u_first, u_second in orders:
        print(f"\n=== order {order_label} ===")
        tally[order_label] = {"A": 0, "B": 0, "malformed": 0}
        for seed in range(1, args.seeds + 1):
            t = time.time()
            try:
                letter, raw, cached = judge.query(
                    question, u_first, u_second, seed=seed, id_slot=args.slot)
            except Exception as e:
                print(f"  seed {seed}: QUERY FAILED: {e}")
                tally[order_label]["malformed"] += 1
                continue
            ms = (time.time() - t) * 1000.0
            key = letter if letter else "malformed"
            tally[order_label][key] += 1
            print(f"  seed {seed:3d}: vote={letter!r:6} {ms:7.0f} ms  "
                  f"cached={cached}  raw={raw[:80]!r}")

    print("\n=== summary ===")
    for order_label, counts in tally.items():
        total = sum(counts.values())
        print(f"  {order_label}: A={counts['A']} B={counts['B']} "
              f"malformed={counts['malformed']} (of {total})")
    if len(tally) == 2:
        a, b = list(tally.values())
        flip = (a["A"] + b["B"]) - (a["B"] + b["A"])
        print(f"  position-consistency: {'consistent' if flip >= 0 else 'INCONSISTENT'} "
              f"(same-image-wins delta {flip:+d})")
    print("[manual_test] done")
    return 0


if __name__ == "__main__":
    sys.exit(main())
