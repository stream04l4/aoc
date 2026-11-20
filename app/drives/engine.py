"""Drive Engine facade: coordinates the 3 independent evaluators, calculator, and arbitrator."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from app.core.config import get_drive_config
from app.core.enums import DriveType
from app.drives.arbitration import DriveArbitrator
from app.drives.calculator import SatisfactionCalculator
from app.drives.curiosity import CuriosityDriveEvaluator
from app.drives.interfaces import (
    DriveContext,
    DriveDecision,
    DriveEngineInterface,
    DriveEvaluator,
    DriveState,
)
from app.drives.reproduction import ReproductionDriveEvaluator
from app.drives.survival import SurvivalDriveEvaluator
from app.observability.metrics import DRIVE_PRIORITY, DRIVE_SATISFACTION


class DriveEngine(DriveEngineInterface):
    def __init__(self, config: dict[str, Any] | None = None) -> None:
        self.config = config if config is not None else get_drive_config()
        drives_cfg = self.config.get("drives", {})
        self.evaluators: dict[str, DriveEvaluator] = {
            DriveType.SURVIVAL.value: SurvivalDriveEvaluator(drives_cfg.get("survival", {})),
            DriveType.REPRODUCTION.value: ReproductionDriveEvaluator(drives_cfg.get("reproduction", {})),
            DriveType.CURIOSITY.value: CuriosityDriveEvaluator(drives_cfg.get("curiosity", {})),
        }
        self.calculator = SatisfactionCalculator(self.config.get("dynamics", {}), drives_cfg)
        self.arbitrator = DriveArbitrator(self.config.get("arbitration", {}), drives_cfg)

    def compute_states(
        self, ctx: DriveContext, previous: dict[str, DriveState]
    ) -> dict[str, DriveState]:
        result: dict[str, DriveState] = {}
        for dtype, evaluator in self.evaluators.items():
            ev = evaluator.evaluate(ctx)
            st = self.calculator.compute(ev, previous.get(dtype), ctx.now)
            result[dtype] = st
            DRIVE_SATISFACTION.labels(drive=dtype).set(st.satisfaction)
        return result

    def arbitrate(
        self,
        states: dict[str, DriveState],
        constraints: dict[str, Any] | None = None,
        now: datetime | None = None,
    ) -> DriveDecision:
        decision = self.arbitrator.arbitrate(states, now=now, constraints=constraints)
        for dtype, info in decision.scores.items():
            DRIVE_PRIORITY.labels(drive=dtype).set(info["priority"])
        return decision
