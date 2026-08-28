"""Backend smoke test: load a tourney, check state_dict fields, stop/resume
flags, graph + feed data. Read-only except the stop/resume flip (restored).

Usage (repo root):
    python tests/test_state.py [tourney]
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
print("loaded:", s["loaded"])
print("running:", s["running"], "stopping:", s["stopping"])
print("phase_label:", s["phase_label"])
if not s["ranking"]:
    print("ranking empty (no battles yet) — nothing else to check")
    sys.exit(0)
print("ranking[0] keys:", sorted(s["ranking"][0].keys()))
r0 = s["ranking"][0]
print(f"  top image: {r0['name'][:30]} theta={r0['theta']} rr={r0['rr_done']}/{r0['rr_total']}")
print("graph nodes:", len(s["graph"]["nodes"]), "edges:", len(s["graph"]["edges"]))
print("recent_matches:", len(s["recent_matches"]))
print("pivot:", s["pivot"])

# Test stop/resume flags
a.do_stop()
print("after do_stop: stop_after_match =", a.stop_after_match)
a.do_resume()
print("after do_resume: _running =", a._running)
a.do_stop()
print("OK")
