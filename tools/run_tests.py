#!/usr/bin/env python3
"""
Run every test suite and report one aggregate result.

    python3 tools/run_tests.py

The same entry point CI uses, so a green run here means a green run there. Exits
non-zero if any suite fails.

Suites are plain scripts rather than pytest cases: they need no dependencies
beyond boto3, they run in about a second, and they can be executed individually
while debugging a single handler.
"""

import glob
import os
import re
import subprocess
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
COUNT_RE = re.compile(r"^(\d+)/(\d+) passed$", re.MULTILINE)


def main():
    suites = sorted(glob.glob(os.path.join(REPO_ROOT, "tests", "test_*.py")))
    if not suites:
        print("No test suites found under tests/", file=sys.stderr)
        return 1

    total_passed = total_run = 0
    failed_suites = []

    for path in suites:
        name = os.path.relpath(path, REPO_ROOT)
        result = subprocess.run(
            [sys.executable, path],
            capture_output=True,
            text=True,
            cwd=REPO_ROOT,
        )
        output = result.stdout + result.stderr

        match = COUNT_RE.search(result.stdout)
        passed, run = (int(match.group(1)), int(match.group(2))) if match else (0, 0)
        total_passed += passed
        total_run += run

        if result.returncode == 0:
            print(f"  PASS  {name:<34} {passed}/{run}")
        else:
            failed_suites.append(name)
            print(f"  FAIL  {name:<34} {passed}/{run}")
            # Only failing suites print detail — a green run stays quiet.
            for line in output.splitlines():
                if "[FAIL]" in line or "Error" in line or "Traceback" in line:
                    print(f"        {line.strip()}")

    print(f"\n{total_passed}/{total_run} checks passed across {len(suites)} suite(s)")

    if failed_suites:
        print(f"Failing suites: {', '.join(failed_suites)}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
