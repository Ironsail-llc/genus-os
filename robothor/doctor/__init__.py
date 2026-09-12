"""``genus doctor`` -- one answer to "is this instance actually working?".

Before this package the question had three partial answers and no whole one.
``genus config validate`` checked three environment variables, four ports and a
handful of endpoints, and could repair nothing. ``scripts/instance_doctor.sh``
printed ``FINDING`` lines that nothing parsed. ``/ready`` answered for one
service at a time. None of them could see the defects that actually bite a
fresh install: a database whose ``service`` role was never seeded, so every
scheduled agent is denied every tool; a migration ledger that has drifted; a
manifest the schema would reject; an owner account that does not exist; a
secrets backend that refused; a container that cannot make an LLM call at all.

The shape is a registry of small, time-boxed, independently-testable checks:

    from robothor.doctor.context import DoctorContext
    from robothor.doctor.runner import run_sync

    report = run_sync(DoctorContext())
    report.exit_code   # 0 healthy, 1 a required check failed, 2 doctor errored
    report.as_dict()   # what `--json` prints and what GET /api/doctor serves

Three properties are load-bearing and each has tests that would fail loudly:
importing this package needs no database and no engine; no check can hang the
run; and no check prints a credential -- fingerprints and sources only.
"""

from __future__ import annotations

from robothor.doctor.context import DoctorContext
from robothor.doctor.model import Check, FixResult, Result
from robothor.doctor.runner import CheckResult, DoctorReport, run, run_sync

__all__ = [
    "Check",
    "CheckResult",
    "DoctorContext",
    "DoctorReport",
    "FixResult",
    "Result",
    "run",
    "run_sync",
]
