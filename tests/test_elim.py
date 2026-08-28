"""Test elimination persistence + eliminated-in-ranking + gpu info.

Mutates out/{tourney}/eliminated.json but un-eliminates (restores) at the
end, so it does not leave test state behind.

Usage (repo root):
    python tests/test_elim.py [tourney]
"""
import json
import sys

from src.arena import Arena

TOURNEY = sys.argv[1] if len(sys.argv) > 1 else "default"

a = Arena()
a.load_tourney(TOURNEY)
if not a.is_loaded:
    print(f"tourney {TOURNEY!r} not loadable — put images in in/ (or "
          f"in/{TOURNEY}/) first")
    sys.exit(0)

# Pick a low-ranked active image and eliminate it
s = a.state_dict()
if not s["ranking"]:
    print("ranking empty (no battles yet) — nothing to eliminate")
    sys.exit(0)
active = [r for r in s["ranking"] if not r["eliminated"]]
if not active:
    print("no active images to eliminate")
    sys.exit(0)
victim = active[-1]["key"]
print("Eliminating:", victim.split("\\")[-1])
a.do_eliminate(victim)

# Check the sidecar file
elim_path = a.out_dir / "eliminated.json"
print("eliminated.json exists:", elim_path.exists())
print("eliminated.json content count:", len(json.loads(elim_path.read_text())))

# Check ranking now includes eliminated at bottom
s2 = a.state_dict()
elim_rows = [r for r in s2["ranking"] if r["eliminated"]]
active_rows = [r for r in s2["ranking"] if not r["eliminated"]]
print(f"ranking: {len(active_rows)} active + {len(elim_rows)} eliminated = {len(s2['ranking'])} total")
print("last row eliminated?", s2["ranking"][-1]["eliminated"])

# GPU info
gpu = s2.get("gpu")
print("gpu info:", gpu)

# Simulate a full reload (new Arena instance)
b = Arena()
b.load_tourney(TOURNEY)
s3 = b.state_dict()
elim3 = [r for r in s3["ranking"] if r["eliminated"]]
print(f"after reload: {len(elim3)} eliminated restored")
print("restored victim matches:", any(r["key"] == victim for r in elim3))

# Clean up: un-eliminate (remove from file) so we don't leave test state
if elim_path.exists():
    keys = json.loads(elim_path.read_text())
    if victim in keys:
        keys.remove(victim)
        elim_path.write_text(json.dumps(keys))
        print("cleaned up test elimination")
print("OK")
