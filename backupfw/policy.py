"""정책 파일을 읽고 검사한다.

정책은 담당자가 쓰는 유일한 파일이다. 여기서 하는 검사가 곧 CI 가 merge 전에
PR 에 남기는 코멘트에 해당한다.
"""
from __future__ import annotations

from datetime import date
from pathlib import Path

import yaml

from .config import ROOT, parse_duration, roles, schedule_interval

JUDGEMENTS = ("backup", "regenerate", "exclude", "hold")
KINDS = ("data", "config", "log")


class Item:
    def __init__(self, policy: "Policy", data: dict):
        self.policy = policy
        self.d = data or {}

    def __repr__(self) -> str:
        return f"<Item {self.policy.service}/{self.name} {self.judgement}>"

    name = property(lambda self: self.d.get("name", "?"))
    kind = property(lambda self: self.d.get("kind"))
    judgement = property(lambda self: self.d.get("judgement"))
    reason = property(lambda self: self.d.get("reason"))
    source = property(lambda self: self.d.get("source") or {})
    adapter = property(lambda self: self.d.get("adapter"))
    schedule = property(lambda self: self.d.get("schedule"))
    retention = property(lambda self: self.d.get("retention") or {})
    target = property(lambda self: self.d.get("target"))
    objectives = property(lambda self: self.d.get("objectives") or {})
    restore_test = property(lambda self: self.d.get("restore_test") or {})
    regenerate = property(lambda self: self.d.get("regenerate") or {})
    review_by = property(lambda self: self.d.get("review_by"))
    review_role = property(lambda self: self.d.get("review_role"))

    @property
    def verify_data(self) -> list:
        return (self.d.get("verify") or {}).get("data") or []

    @property
    def with_leader(self) -> str | None:
        """'with:database' 면 'database' 를 돌려준다. 같은 실행에 묶인다."""
        s = str(self.schedule or "")
        return s[len("with:"):] if s.startswith("with:") else None

    @property
    def is_leader(self) -> bool:
        return self.judgement == "backup" and self.with_leader is None

    @property
    def effective_schedule(self) -> str | None:
        if self.with_leader:
            leader = self.policy.item(self.with_leader)
            return leader.schedule if leader else None
        return self.schedule

    @property
    def interval(self):
        return schedule_interval(self.effective_schedule)


class Policy:
    def __init__(self, path: Path, data: dict):
        self.path = path
        self.d = data or {}
        self.items = [Item(self, x) for x in (self.d.get("items") or [])]

    service = property(lambda self: self.d.get("service", "?"))
    owner_role = property(lambda self: self.d.get("owner_role"))
    tier = property(lambda self: self.d.get("tier"))

    def item(self, name: str) -> Item | None:
        for it in self.items:
            if it.name == name:
                return it
        return None

    def leaders(self) -> list[Item]:
        """스케줄이 생기는 항목. judgement: backup 이고 다른 항목에 묶이지 않은 것."""
        return [it for it in self.items if it.is_leader]

    def members(self, leader_name: str) -> list[Item]:
        """한 실행에서 함께 백업되는 항목들. 하나의 backup_id 로 묶인다."""
        leader = self.item(leader_name)
        if leader is None:
            raise KeyError(f"{self.service} 에 {leader_name} 항목이 없다")
        rest = [it for it in self.items if it.with_leader == leader_name]
        return [leader, *rest]


def policy_dir() -> Path:
    return ROOT / "policies"


def load_policy(service: str) -> Policy:
    path = policy_dir() / f"{service}.yaml"
    if not path.exists():
        raise FileNotFoundError(f"정책 파일이 없다: {path}")
    return Policy(path, yaml.safe_load(path.read_text(encoding="utf-8")))


def load_all_policies() -> list[Policy]:
    out = []
    for p in sorted(policy_dir().glob("*.yaml")):
        if p.name.startswith("_"):
            continue
        out.append(Policy(p, yaml.safe_load(p.read_text(encoding="utf-8"))))
    return out


# ── 검사 ─────────────────────────────────────────────────────────────

def validate(policy: Policy) -> list[str]:
    """merge 전에 잡아야 할 것들. 실패 메시지는 고칠 곳을 가리켜야 한다."""
    errs: list[str] = []

    if not policy.service:
        errs.append("service 가 없다")
    if not policy.owner_role:
        errs.append("owner_role 이 없다 — 개인 이름이 아니라 역할명을 적는다")
    elif policy.owner_role not in roles():
        errs.append(f"owner_role '{policy.owner_role}' 이 roles.yaml 에 없다")

    if not policy.items:
        errs.append("items 가 비었다 — 판정하지 않은 서비스로 남는다")

    names = set()
    for i, it in enumerate(policy.items):
        at = f"items[{i}] ({it.name})"

        if not it.name:
            errs.append(f"{at} name 이 없다")
        if it.name in names:
            errs.append(f"{at} 이름이 중복된다")
        names.add(it.name)

        if it.kind not in KINDS:
            errs.append(f"{at} kind 는 {'|'.join(KINDS)} 중 하나 (지금: {it.kind})")
        if it.judgement not in JUDGEMENTS:
            errs.append(f"{at} judgement 는 {'|'.join(JUDGEMENTS)} 중 하나 (지금: {it.judgement})")
        if not it.reason and it.judgement in ("exclude", "hold", "backup", "regenerate"):
            errs.append(f"{at} reason 이 없다 — 판정 근거를 남긴다")

        if it.judgement == "backup":
            for field in ("adapter", "target"):
                if not getattr(it, field):
                    errs.append(f"{at} judgement: backup 이면 {field} 가 필요하다")
            if not it.schedule:
                errs.append(f"{at} judgement: backup 이면 schedule 이 필요하다")
            if it.is_leader and not it.retention:
                errs.append(f"{at} judgement: backup 이면 retention 이 필요하다")
            if it.with_leader and policy.item(it.with_leader) is None:
                errs.append(f"{at} with:{it.with_leader} 대상 항목이 없다")

        if it.judgement == "regenerate":
            for field in ("procedure", "duration"):
                if not it.regenerate.get(field):
                    errs.append(f"{at} regenerate.{field} 가 필요하다 — 복구 시간에 포함된다")

        if it.judgement == "hold":
            if not it.review_by:
                errs.append(f"{at} hold 는 review_by 가 필요하다 — 무기한 보류를 막는다")
            if not it.review_role:
                errs.append(f"{at} hold 는 review_role 이 필요하다")

    return errs


def check_objectives(item: Item, caps: dict) -> str | None:
    """정책의 목표를 어댑터가 감당할 수 있는지. 못 하면 실행 전에 막는다."""
    rpo = parse_duration(item.objectives.get("rpo"))
    interval = item.interval
    if rpo and interval and rpo < interval and not caps.get("pitr"):
        return (f"rpo {item.objectives.get('rpo')} 가 백업 주기보다 짧은데 "
                f"어댑터 {caps.get('adapter')} 는 특정 시점 복구를 지원하지 않는다")
    return None


def overdue_reviews(policy: Policy, today: date | None = None) -> list[Item]:
    today = today or date.today()
    out = []
    for it in policy.items:
        if it.judgement != "hold" or not it.review_by:
            continue
        rb = it.review_by
        if isinstance(rb, str):
            try:
                rb = date.fromisoformat(rb)
            except ValueError:
                continue
        if rb < today:
            out.append(it)
    return out
