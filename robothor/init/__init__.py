"""The ``genus init`` wizard: two phases, resumable steps, one substrate.

``robothor.setup`` keeps the public names an installer and the tests import
(``run_init``, ``create_workspace``, ``check_prerequisites``) and orchestrates
this package. Everything the wizard decides lives here.
"""

from __future__ import annotations

from robothor.init.context import InitContext
from robothor.init.plan import InitPlan, InitResult, PlanEntry, StepOutcome, run_plan
from robothor.init.steps import BaseStep, CheckResult, Step, StepError

__all__ = [
    "BaseStep",
    "CheckResult",
    "InitContext",
    "InitPlan",
    "InitResult",
    "PlanEntry",
    "Step",
    "StepError",
    "StepOutcome",
    "run_plan",
]
