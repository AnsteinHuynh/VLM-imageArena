"""Compare bootstrap CI vs Fisher CI on a real battles.jsonl log.

Usage (repo root):
    python tests/test_bootstrap.py [tourney]

`tourney` defaults to "default" (the legacy in/ + out/ slot).
"""
import sys
import time
from pathlib import Path

from src import config
from src.bt_model import BTModel

tourney = sys.argv[1] if len(sys.argv) > 1 else "default"
path = Path(config.OUT_DIR) / tourney / "battles.jsonl"
if not path.is_file():
    print(f"no battle log at {path} — run a tourney first, or pass a name: "
          f"python tests/test_bootstrap.py my-tourney")
    sys.exit(0)

bt = BTModel()
bt.replay_log(path)
bt.fit()

# Bootstrap CI (timed)
t0 = time.time()
ci_boot = bt.bootstrap_ci(n_iter=100)
t_boot = time.time() - t0

# Fisher CI
t0 = time.time()
ci_fish = bt.confidence_intervals()
t_fish = time.time() - t0

print(f"Bootstrap CI (100 iters): {t_boot:.2f}s")
print(f"Fisher CI: {t_fish:.3f}s")
print()
print(f"{'image':42s} {'theta':>8} {'bootCI':>8} {'fishCI':>10} {'games':>6}")
print("-" * 80)
for key, theta, games in bt.ranking()[:12]:
    name = key.split("\\")[-1][:40]
    print(f"{name:42s} {theta:>+8.3f} {ci_boot[key]:>8.3f} {ci_fish[key]:>10.3f} {games:>6}")
