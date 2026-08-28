"""Smoke test launcher."""
from src.arena import Arena
a = Arena()
print(f"Loaded: {a.is_loaded}")
tourneys = a.list_tourneys()
print(f"Available tourneys ({len(tourneys)}):")
for t in tourneys:
    name = t["name"]
    count = t["image_count"]
    log = t["has_log"]
    print(f"  {name:35s} {count:3d} images  log={log}")
