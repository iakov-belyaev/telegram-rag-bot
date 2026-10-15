"""Verification of control_cli.py: flags roundtrip, CLI, watcher transitions."""
import pathlib as _pathlib
import sys
import tempfile
from types import SimpleNamespace

sys.path.insert(0, str(_pathlib.Path(__file__).resolve().parent.parent))

import config

TMP = _pathlib.Path(tempfile.mkdtemp(prefix="control_test_"))
config.STATE_DIR = TMP
config.CONTROL_FLAGS_FILE = TMP / "control_flags.json"

import control_cli

FAIL = []


def check(name, cond, extra=""):
    print(f"{'PASS' if cond else 'FAIL'}  {name} {extra}")
    if not cond:
        FAIL.append(name)


class FakeControl:
    def __init__(self):
        self.calls = []

    def pause(self, reason):
        self.calls.append(("pause", reason))

    def resume(self):
        self.calls.append(("resume",))

    def request_stop(self):
        self.calls.append(("stop",))


# CLI roundtrip
check("cli pause", control_cli._cli(["pause"]) == 0)
check("flags pause set", control_cli.get_flags()["pause"] is True)
check("cli status", control_cli._cli(["status"]) == 0)
check("cli resume", control_cli._cli(["resume"]) == 0)
check("flags cleared", control_cli.get_flags()["pause"] is False)
check("cli bad usage", control_cli._cli(["nonsense"]) == 2)

# watcher transitions (pause -> resume -> stop)
ctrl = FakeControl()
w = control_cli.FlagWatcher()
control_cli.set_flag("clear" if False else "pause", False)  # ensure clean base
w.apply(ctrl)                        # baseline: no flags
control_cli.set_flag("pause", True)
w.apply(ctrl)                        # pause
control_cli.set_flag("pause", False)
w.apply(ctrl)                        # resume
control_cli.set_flag("stop", True)
w.apply(ctrl)                        # stop
check("watcher sequence", ctrl.calls == [("pause", "flags"), ("resume",), ("stop",)],
      extra=str(ctrl.calls))

# clear command
control_cli._cli(["clear"])
check("clear resets", control_cli.get_flags()["stop"] is False)

print()
if FAIL:
    print("FAILURES:", FAIL)
    sys.exit(1)
print("ALL CONTROL CLI CHECKS PASSED")
