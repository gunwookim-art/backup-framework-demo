"""명령줄 도구.

    python -m backupfw lint                 정책 검사 (CI 가 merge 전에 하는 일)
    python -m backupfw render               정책에서 생길 스케줄 보기
    python -m backupfw backup <svc> <item>  백업 1회
    python -m backupfw restore-test <svc> <item>
    python -m backupfw check                점검 · 공백 목록
"""
from __future__ import annotations

import argparse
import sys

from .checker import compute_gaps, sweep_stale_running, summarize
from .backends import results_store
from .policy import load_all_policies, validate

OK = "  ok  "
BAD = " fail "


def cmd_lint(_args) -> int:
    failed = 0
    for policy in load_all_policies():
        errors = validate(policy)
        if errors:
            failed += 1
            print(f"[{BAD}] {policy.path.name}")
            for e in errors:
                print(f"         {e}")
        else:
            leaders = len(policy.leaders())
            print(f"[{OK}] {policy.path.name}  항목 {len(policy.items)}개 · 스케줄 {leaders}개")
    if failed:
        print(f"\n{failed}개 정책이 검사를 통과하지 못했다. merge 전에 고쳐야 한다.")
    return 1 if failed else 0


def cmd_render(_args) -> int:
    """정책 → 실행. judgement: backup 인 항목만 스케줄이 생긴다."""
    for policy in load_all_policies():
        print(f"\n{policy.path.name}  (담당 역할 {policy.owner_role})")
        for item in policy.items:
            if item.is_leader:
                members = [i.name for i in policy.members(item.name)]
                print(f"  스케줄 생성  {item.schedule:<14} {policy.service}/{item.name}"
                      f"   묶음: {', '.join(members)}")
                if item.restore_test.get("schedule"):
                    print(f"  스케줄 생성  {item.restore_test['schedule']:<14} "
                          f"{policy.service}/{item.name} (복구 시험)")
            elif item.with_leader:
                continue
            else:
                print(f"  생성 안 함   {item.judgement:<14} {policy.service}/{item.name}"
                      f"   — {item.reason}")
    print("\n판정이 backup 인 항목만 실행이 생긴다. 나머지는 판정과 사유로만 남는다.")
    return 0


def cmd_backup(args) -> int:
    from .runner import run_backup
    result = run_backup(args.service, args.item)
    _print_result(result)
    return 0 if result.status == "success" else 1


def cmd_restore_test(args) -> int:
    from .runner import run_restore_test
    result = run_restore_test(args.service, args.item)
    _print_result(result)
    return 0 if result.status == "success" else 1


def _print_result(result) -> None:
    print(f"\n  {result.run_id}")
    print(f"  상태 {result.status}" + (f" — {result.error}" if result.error else ""))
    for step in result.steps:
        mark = {"ok": "+", "failed": "x", "blocked": "!", "skipped": "-"}.get(step["status"], "?")
        note = f"  {step['note']}" if step.get("note") else ""
        print(f"    {mark} {step['id']:<18}{note}")


def cmd_check(_args) -> int:
    store = results_store()
    closed = sweep_stale_running(store)
    if closed:
        print(f"돌아오지 못한 실행 {len(closed)}건을 마감했다: {', '.join(closed)}")

    gaps = compute_gaps()
    if not gaps:
        print("공백 없음.")
        return 0

    print(f"\n안 되고 있는 것 {len(gaps)}건\n")
    width = max(len(g["kind"]) for g in gaps)
    for gap in gaps:
        where = gap["service"] + (f"/{gap['item']}" if gap["item"] else "")
        print(f"  [{gap['severity']:<8}] {gap['kind']:<{width}}  {where}")
        print(f"               {gap['detail']}")
    return 0


def cmd_services(_args) -> int:
    data = summarize()
    for s in data["services"]:
        owners = ", ".join(s["owners"]) or "(없음)"
        print(f"\n{s['service']}   역할 {s['owner_role']} → {owners}")
        print(f"  판정 {s['judgements']}")
        for line in s["scheduled"]:
            print(f"  {line['item']:<12} {str(line['schedule']):<14} "
                  f"최근 {line['last_status']} · 검증 {line['verified']}")
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="backupfw", description="정책 기반 백업 틀")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("lint", help="정책 검사").set_defaults(fn=cmd_lint)
    sub.add_parser("render", help="정책에서 생길 스케줄 보기").set_defaults(fn=cmd_render)
    sub.add_parser("check", help="점검 · 공백 목록").set_defaults(fn=cmd_check)
    sub.add_parser("services", help="서비스별 현황").set_defaults(fn=cmd_services)

    p = sub.add_parser("backup", help="백업 1회")
    p.add_argument("service")
    p.add_argument("item")
    p.set_defaults(fn=cmd_backup)

    p = sub.add_parser("restore-test", help="격리 복구 시험")
    p.add_argument("service")
    p.add_argument("item")
    p.set_defaults(fn=cmd_restore_test)

    args = parser.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
