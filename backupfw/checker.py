"""점검 — 정책(있어야 할 것)과 결과 기록(실제 있었던 것)을 맞댄다.

여기서 나오는 목록이 이 틀의 핵심이다.
백업 작업이 등록되어 있다는 것과 복구할 수 있다는 것은 별개이므로,
'작업이 있다' 가 아니라 '최근 성공 기록이 있고 검증을 통과했다' 로 판정한다.

점검 자체는 백업을 하지 않는다. 훑어보기만 한다.
"""
from __future__ import annotations

from datetime import date, datetime, timezone

from .backends import (artifact_backend_names, results_store,
                       scan_artifact_tree)
from .config import inventory, roles, utcnow
from .policy import load_all_policies, overdue_reviews

STALE_RUNNING_HOURS = 12
FAIL_STREAK = 2


def _sev(kind: str) -> str:
    critical = {"미실행", "산출물 이상", "실패 지속"}
    warning = {"미검증", "미등록 실행", "미승계", "미등록 서비스"}
    return "critical" if kind in critical else ("warning" if kind in warning else "info")


def _gap(kind: str, service: str, item: str | None, detail: str, **extra) -> dict:
    return {"kind": kind, "severity": _sev(kind), "service": service,
            "item": item, "detail": detail, **extra}


def _parse_iso(text: str | None) -> datetime | None:
    if not text:
        return None
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def sweep_stale_running(store) -> list[dict]:
    """돌아오지 못한 실행을 마감한다.

    시작 기록을 먼저 남기기 때문에, 'running 인 채로 오래된 것' 이
    '강제 종료된 실행' 으로 드러난다. 끝에만 기록했다면 아무 흔적도 없었을 것이다.
    """
    closed = []
    now = utcnow()
    for run in store.list_runs():
        if run.get("status") != "running":
            continue
        started = _parse_iso(run.get("started_at"))
        if started and (now - started).total_seconds() > STALE_RUNNING_HOURS * 3600:
            run["status"] = "failed"
            run["error"] = f"{STALE_RUNNING_HOURS}시간 넘게 끝나지 않아 점검이 마감함"
            run["ended_at"] = now.isoformat(timespec="seconds")
            store.backend.write_json(
                f"{store.prefix}/{run['policy']['service']}/{run['policy']['item']}"
                f"/{run['run_id']}.json", run)
            closed.append(run["run_id"])
    return closed


def compute_gaps() -> list[dict]:
    store = results_store()
    policies = load_all_policies()
    known = {p.service: p for p in policies}
    role_map = roles()
    now = utcnow()
    today = date.today()
    gaps: list[dict] = []

    # 1. 미등록 서비스 — 존재하는데 정책 파일이 없다
    for service in inventory():
        if service not in known:
            gaps.append(_gap("미등록 서비스", service, None,
                             "서비스는 있는데 정책 파일이 없다 — 보호 여부가 판정된 적 없음"))

    for policy in policies:
        # 2. 미승계 — 역할에 배정된 사람이 0명
        holders = role_map.get(policy.owner_role) or []
        if not holders:
            gaps.append(_gap("미승계", policy.service, None,
                             f"담당 역할 '{policy.owner_role}' 에 배정된 사람이 없다",
                             owner_role=policy.owner_role))

        # 3. 재검토 초과 — hold 인데 기한이 지났다
        for item in overdue_reviews(policy, today):
            gaps.append(_gap("재검토 초과", policy.service, item.name,
                             f"보류 상태로 재검토 기한 {item.review_by} 이 지났다",
                             review_role=item.review_role))

        # 4~7. 백업 대상별 상태
        for item in policy.leaders():
            latest = store.read_latest(policy.service, item.name)
            interval = item.interval

            if latest is None:
                gaps.append(_gap("미실행", policy.service, item.name,
                                 "성공한 백업 기록이 한 번도 없다"))
            else:
                ended = _parse_iso(latest.get("ended_at") or latest.get("started_at"))
                if interval and ended:
                    overdue = now - ended
                    if overdue.total_seconds() > interval.total_seconds() * 1.5:
                        hours = int(overdue.total_seconds() // 3600)
                        gaps.append(_gap("미실행", policy.service, item.name,
                                         f"마지막 성공이 {hours}시간 전 — 예정 간격의 1.5배를 넘었다"))

                empty = [a.get("file") for a in latest.get("artifacts", [])
                         if not a.get("size")]
                if empty:
                    gaps.append(_gap("산출물 이상", policy.service, item.name,
                                     f"크기가 0인 산출물 — {', '.join(map(str, empty))}"))

                if latest.get("verification", {}).get("data") != "pass":
                    gaps.append(_gap("미검증", policy.service, item.name,
                                     "복구해서 확인한 적이 없다 — 작업 성공은 복구 가능과 별개"))

            runs = store.list_runs(policy.service, item.name)
            streak = 0
            for run in runs:
                if run.get("status") in ("failed", "blocked"):
                    streak += 1
                elif run.get("status") == "success":
                    break
            if streak >= FAIL_STREAK:
                last = runs[0].get("error") if runs else ""
                gaps.append(_gap("실패 지속", policy.service, item.name,
                                 f"최근 {streak}회 연속 실패 — {last}"))

        # 산출물은 있는데 지금 정책에 없는 항목
        expected_items = {i.name for i in policy.items}
        for backend_name in artifact_backend_names():
            for service, items in scan_artifact_tree(backend_name).items():
                if service != policy.service:
                    continue
                for found in items:
                    if found not in expected_items:
                        gaps.append(_gap("미등록 실행", service, found,
                                         f"{backend_name} 에 산출물이 있는데 정책에 없는 항목이다"))

    # 8. 정책이 아예 없는 서비스의 산출물
    for backend_name in artifact_backend_names():
        for service, items in scan_artifact_tree(backend_name).items():
            if service not in known:
                gaps.append(_gap("미등록 실행", service, ", ".join(items),
                                 f"{backend_name} 에 산출물이 쌓이는데 대응하는 정책이 없다"))

    order = {"critical": 0, "warning": 1, "info": 2}
    gaps.sort(key=lambda g: (order[g["severity"]], g["service"], g["item"] or ""))
    return gaps


def summarize() -> dict:
    store = results_store()
    policies = load_all_policies()
    services = []
    for policy in policies:
        counts: dict[str, int] = {}
        for item in policy.items:
            counts[item.judgement] = counts.get(item.judgement, 0) + 1
        leaders = []
        for item in policy.leaders():
            latest = store.read_latest(policy.service, item.name)
            leaders.append({
                "item": item.name,
                "schedule": item.schedule,
                "target": item.target,
                "last_status": (latest or {}).get("status"),
                "last_run": (latest or {}).get("ended_at"),
                "verified": (latest or {}).get("verification", {}).get("data"),
                "backup_id": (latest or {}).get("backup_id"),
            })
        services.append({
            "service": policy.service,
            "owner_role": policy.owner_role,
            "owners": roles().get(policy.owner_role) or [],
            "tier": policy.tier,
            "judgements": counts,
            "scheduled": leaders,
        })
    return {"services": services, "generated_at": utcnow().isoformat(timespec="seconds")}
