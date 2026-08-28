# These are manual smoke scripts with module-level side effects (they load
# real tourney data, flip stop flags, mutate eliminated.json). Keep pytest
# from collecting + executing them as tests — run them directly instead:
#   python tests/test_state.py [tourney]
collect_ignore_glob = ["test_*.py"]
