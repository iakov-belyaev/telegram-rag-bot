"""Verification of scheduler.py: due jobs, stop honouring, error isolation."""
import asyncio
import pathlib as _pathlib
import sys
from types import SimpleNamespace

sys.path.insert(0, str(_pathlib.Path(__file__).resolve().parent.parent))

import metrics
import scheduler as sched_mod

FAIL = []


def check(name, cond, extra=""):
    print(f"{'PASS' if cond else 'FAIL'}  {name} {extra}")
    if not cond:
        FAIL.append(name)


async def main():
    control = SimpleNamespace(stop_requested=False)
    s = sched_mod.Scheduler(control=control)

    ran = {"fast": 0, "boom": 0, "later": 0}

    async def fast_job():
        ran["fast"] += 1

    async def boom_job():
        ran["boom"] += 1
        if ran["boom"] >= 2:
            control.stop_requested = True
        raise RuntimeError("job exploded")

    s.add("fast", 0.05, fast_job, run_immediately=True)
    s.add("boom", 0.05, boom_job, run_immediately=True)

    task = asyncio.create_task(s.run())
    await asyncio.wait_for(task, timeout=10)
    check("scheduler stops on control", ran["fast"] >= 2 and ran["boom"] == 2,
          extra=str(ran))
    st = {j["name"]: j for j in s.status()}
    check("status shape", st["fast"]["runs"] >= 2 and st["boom"]["errors"] == 2, extra=str(st))
    check("error metric", metrics.get_counter("scheduler_job_errors") >= 2)

    # interval gating: a job with a long interval should not run before due
    control2 = SimpleNamespace(stop_requested=False)
    s2 = sched_mod.Scheduler(control=control2)
    calls = {"n": 0}

    async def never_due():
        calls["n"] += 1

    s2.add("slow", 3600, never_due, run_immediately=False)

    async def stopper():
        await asyncio.sleep(1.2)
        control2.stop_requested = True

    asyncio.create_task(stopper())
    await asyncio.wait_for(s2.run(), timeout=10)
    check("not-due job skipped", calls["n"] == 0, extra=str(calls))

    print()
    if FAIL:
        print("FAILURES:", FAIL)
        sys.exit(1)
    print("ALL SCHEDULER CHECKS PASSED")


asyncio.run(main())
