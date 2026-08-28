"""Smoke test: verify arena + dashboard integration."""
import tempfile, pathlib
from src.arena import Arena, create_dashboard_app, VERSION
from src import config

print(f"Arena version: {VERSION}")
print(f"Config version: {config.VERSION}")
print(f"Dashboard port: {config.DASHBOARD_PORT}")
print(f"RR_CYCLES: {config.RR_CYCLES}")
print(f"EPSILON: {config.EPSILON}")
print(f"JUDGE_BETA: {config.JUDGE_BETA}")

# Verify app can be created
test_dir = pathlib.Path(tempfile.gettempdir()) / "arena_test_dummy"
test_dir.mkdir(exist_ok=True)
arena = Arena(test_dir, test_dir, "test")
app = create_dashboard_app(arena)
print(f"FastAPI app created: {app.title}")
routes = [r.path for r in app.routes if hasattr(r, "path")]
print(f"Routes: {routes}")
print("INTEGRATION TEST OK")
