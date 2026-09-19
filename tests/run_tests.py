#!/usr/bin/env python3
"""Test runner without pytest.

The container has no package index, so the suite runs on the standard library
alone.  This walks ``tests/test_*.py``, calls every ``test_*`` function in
definition order and prints one line per test.  Failures do not stop the run --
the whole picture matters more than the first broken assertion.

Usage:  PYTHONPATH=src python3 tests/run_tests.py [name-fragment ...]
"""

from __future__ import annotations

import importlib.util
import os
import sys
import time
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SRC = os.path.join(ROOT, "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

GREEN = "\033[32m"
RED = "\033[31m"
YELLOW = "\033[33m"
DIM = "\033[2m"
OFF = "\033[0m"
if not sys.stdout.isatty():
    GREEN = RED = YELLOW = DIM = OFF = ""


def load_module(path):
    name = "canopy_tests_" + os.path.splitext(os.path.basename(path))[0]
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def collect(module):
    """Functions in source order -- definition order is the reading order."""
    funcs = [
        (name, obj)
        for name, obj in vars(module).items()
        if name.startswith("test_") and callable(obj)
    ]
    funcs.sort(key=lambda kv: getattr(kv[1], "__code__", None).co_firstlineno)
    return funcs


def main(argv):
    filters = [a for a in argv if not a.startswith("-")]
    files = sorted(
        os.path.join(HERE, f)
        for f in os.listdir(HERE)
        if f.startswith("test_") and f.endswith(".py")
    )
    passed = failed = 0
    failures = []
    started = time.time()

    for path in files:
        try:
            module = load_module(path)
        except Exception:
            print(f"{RED}IMPORT FAIL{OFF} {os.path.basename(path)}")
            traceback.print_exc()
            failed += 1
            failures.append((os.path.basename(path), "import"))
            continue

        print(f"\n{DIM}{os.path.basename(path)}{OFF}")
        for name, func in collect(module):
            if filters and not any(f in name for f in filters):
                continue
            t0 = time.time()
            try:
                func()
            except AssertionError as exc:
                failed += 1
                # a bare `assert x` carries no message; show the line instead
                why = str(exc).strip() or "".join(
                    traceback.format_exc().strip().splitlines()[-2:]
                ).strip()
                failures.append((name, why))
                print(f"  {RED}FAIL{OFF} {name}")
                for line in why.splitlines()[:3]:
                    print(f"       {line[:400]}")
            except Exception as exc:  # noqa: BLE001 -- a crash is a failure too
                failed += 1
                failures.append((name, f"{type(exc).__name__}: {exc}"))
                print(f"  {RED}ERROR{OFF} {name}")
                for line in traceback.format_exc().strip().splitlines()[-6:]:
                    print(f"       {line}")
            else:
                passed += 1
                dt = time.time() - t0
                mark = f" {DIM}{dt:.2f}s{OFF}" if dt > 0.25 else ""
                doc = (func.__doc__ or "").strip().splitlines()
                label = f" {DIM}{doc[0][:70]}{OFF}" if doc else ""
                print(f"  {GREEN}ok{OFF}   {name}{label}{mark}")

    total = time.time() - started
    print()
    if failed:
        print(f"{RED}{failed} failed{OFF}, {passed} passed in {total:.1f}s")
        for name, why in failures:
            print(f"  - {name}: {(why.splitlines() or [''])[0][:160]}")
        return 1
    print(f"{GREEN}{passed} passed{OFF} in {total:.1f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
