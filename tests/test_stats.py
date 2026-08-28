"""Verify the dashboard stats fields + bootstrap CI in state_dict.

Usage (repo root):
    python tests/test_stats.py [tourney]
"""
import sys

from src.arena import Arena

TOURNEY = sys.argv[1] if len(sys.argv) > 1 else "default"

a = Arena()
a.load_tourney(TOURNEY)
if not a.is_loaded:
    print(f"tourney {TOURNEY!r} not loadable — put images in in/ (or "
          f"in/{TOURNEY}/) first")
    sys.exit(0)
s = a.state_dict()

print("== stats ==")
for k in ["queries_per_sec", "games_per_sec", "games_ratio", "starvation", "wasted_matches", "pixel_budget"]:
    print(f"  {k}: {s['stats'].get(k)}")

print("\n== top-5 ranking (CI should be clean, ~0.1-0.4) ==")
for r in s["ranking"][:5]:
    print(f"  {r['name'][:35]:35s} theta={r['theta']:+.3f} CI=[{r['ci_lower']:+.3f}, {r['ci_upper']:+.3f}] ci={r['ci']:.3f}")

print("\npixel_budget:", s["stats"]["pixel_budget"], "=", int(s["stats"]["pixel_budget"]**0.5), "x", int(s["stats"]["pixel_budget"]**0.5))
