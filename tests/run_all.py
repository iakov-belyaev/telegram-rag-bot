"""Run every test script in tests/ and report a summary.

Usage: python tests/run_all.py   (also used by CI)
"""

import pathlib
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parent
TEST_ORDER = [
    "test_database.py", "test_scraper.py", "test_profiles.py", "test_phase1_fixes.py",
    "test_rbac.py", "test_bot.py", "test_api.py", "test_main.py", "test_quality.py",
    "test_discovery.py", "test_storage.py", "test_query.py", "test_watchlist.py",
    "test_scheduler.py", "test_control_cli.py", "test_export.py", "test_wiring.py",
    "test_live_aggregate.py",
]


def main() -> int:
    results = {}
    for name in TEST_ORDER:
        path = ROOT / name
        if not path.exists():
            results[name] = "SKIP (missing)"
            continue
        proc = subprocess.run([sys.executable, str(path)], capture_output=True, text=True)
        results[name] = "PASS" if proc.returncode == 0 else "FAIL"
        if proc.returncode != 0:
            print(f"--- {name} FAILED (tail) ---")
            print("\n".join((proc.stdout or "").splitlines()[-15:]))
            print((proc.stderr or "")[-800:])
    print("\n=== TEST SUMMARY ===")
    for name, status in results.items():
        print(f"{status:14s} {name}")
    failed = [n for n, s in results.items() if s == "FAIL"]
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
