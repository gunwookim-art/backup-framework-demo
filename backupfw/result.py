"""실행 결과 기록.

모든 결과는 {service, item, policy_version} 을 갖는다. 이 키로 정책과 조인한다.
정책 저장소에는 결과를 쓰지 않는다 — 정책 변경 이력이 실행 기록으로 오염된다.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from .config import utcnow


class Blocked(Exception):
    """사전 조건이 맞지 않는다. 백업을 시도하지 않고, 보존 정리도 하지 않는다."""


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat(timespec="seconds") if dt else None


@dataclass
class Result:
    run_id: str
    kind: str                      # backup | restore_test | check
    service: str
    item: str
    policy_version: str = "local"
    owner_role: str | None = None

    status: str = "pending"        # pending running success failed blocked cancelled
    started_at: datetime | None = None
    ended_at: datetime | None = None
    backup_id: str | None = None

    steps: list = field(default_factory=list)
    artifacts: list = field(default_factory=list)
    baseline: dict = field(default_factory=dict)
    measured: dict = field(default_factory=dict)
    observed: dict = field(default_factory=dict)
    retention: dict = field(default_factory=dict)
    objectives: dict = field(default_factory=dict)
    verification: dict = field(default_factory=lambda: {
        "artifact": "not_run", "data": "not_run", "service": "not_run"})
    error: str | None = None

    def step(self, ident: str, status: str, note: str | None = None) -> None:
        self.steps.append({
            "id": ident, "status": status, "note": note,
            "at": _iso(utcnow()),
        })

    @property
    def duration_s(self) -> float | None:
        if self.started_at and self.ended_at:
            return round((self.ended_at - self.started_at).total_seconds(), 2)
        return None

    def to_dict(self) -> dict:
        return {
            "run_id": self.run_id,
            "kind": self.kind,
            "policy": {
                "service": self.service,
                "item": self.item,
                "version": self.policy_version,
                "owner_role": self.owner_role,
            },
            "status": self.status,
            "started_at": _iso(self.started_at),
            "ended_at": _iso(self.ended_at),
            "duration_s": self.duration_s,
            "backup_id": self.backup_id,
            "steps": self.steps,
            "artifacts": self.artifacts,
            "baseline": self.baseline,
            "measured": self.measured,
            "observed": self.observed,
            "retention": self.retention,
            "objectives": self.objectives,
            "verification": self.verification,
            "error": self.error,
        }
