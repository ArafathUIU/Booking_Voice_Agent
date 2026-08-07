"""Run the backend unit tests without pytest (plain asserts).

Usage (from the repo root):  python backend/scripts/run_tests.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import tests.test_conversation_fsm as fsm
import tests.test_latency_defaults as latency


def run(module, name):
    fns = [f for f in dir(module) if f.startswith("test_")]
    for fn in fns:
        getattr(module, fn)()
        print(f"PASS {name}.{fn}")
    return fns


if __name__ == "__main__":
    failed = 0
    for mod, name in ((fsm, "test_conversation_fsm"), (latency, "test_latency_defaults")):
        try:
            fns = run(mod, name)
            print(f"{name}: {len(fns)} tests OK")
        except AssertionError as e:
            print(f"FAIL {name}: {e}")
            failed += 1
    sys.exit(1 if failed else 0)
