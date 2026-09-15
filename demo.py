#!/usr/bin/env python3
"""시나리오 여섯 개를 순서대로 돌려서 틀이 무엇을 잡아내는지 보여준다.

    python demo.py
    python -m backupfw.api      # 그 다음 대시보드
"""
from __future__ import annotations

import shutil
import sqlite3
import sys
from datetime import timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from backupfw.backends import open_backend, results_store  # noqa: E402
from backupfw.checker import compute_gaps  # noqa: E402
from backupfw.cli import cmd_check, cmd_lint, cmd_render  # noqa: E402
from backupfw.config import stamp, utcnow  # noqa: E402
from backupfw.runner import run_backup, run_restore_test  # noqa: E402

DATA = ROOT / "demo-data"
STATE = ROOT / "demo-state"


# ── 화면 ─────────────────────────────────────────────────────────────

def title(n: int, text: str) -> None:
    print(f"\n\n{'━' * 72}\n  {n}. {text}\n{'━' * 72}")


def note(text: str) -> None:
    print(f"\n  → {text}")


def show(result) -> None:
    print(f"\n  {result.run_id}")
    print(f"  상태 {result.status}" + (f"  —  {result.error}" if result.error else ""))
    for step in result.steps:
        mark = {"ok": "+", "failed": "x", "blocked": "!", "skipped": "-"}.get(step["status"], "?")
        extra = f"   {step['note']}" if step.get("note") else ""
        print(f"     {mark} {step['id']:<18}{extra}")


# ── 준비 ─────────────────────────────────────────────────────────────

def seed() -> None:
    """운영 중인 것처럼 보이는 데이터를 만든다."""
    shutil.rmtree(DATA, ignore_errors=True)
    shutil.rmtree(STATE, ignore_errors=True)
    DATA.mkdir(parents=True)

    con = sqlite3.connect(DATA / "orders.db")
    con.execute("create table orders (id integer primary key, customer text, total integer)")
    con.executemany("insert into orders (customer, total) values (?, ?)",
                    [(f"customer-{i:03d}", i * 1000) for i in range(1, 121)])
    con.commit()
    con.close()

    cfg = DATA / "orders-config"
    cfg.mkdir()
    (cfg / "app.yaml").write_text("replicas: 3\ntimeout: 30s\n", encoding="utf-8")
    (cfg / "routes.conf").write_text("location /orders { proxy_pass http://orders; }\n",
                                     encoding="utf-8")

    # billing 은 실제로 billing.db 를 쓴다.
    # 정책은 billing-legacy.db 를 가리킨다 — 경로가 바뀐 뒤 정책이 따라가지 못한 상태.
    con = sqlite3.connect(DATA / "billing.db")
    con.execute("create table invoices (id integer primary key, amount integer)")
    con.executemany("insert into invoices (amount) values (?)", [(i * 500,) for i in range(1, 61)])
    con.commit()
    con.close()


def seed_old_backups() -> None:
    """예전에 쌓인 백업이 있는 것처럼 만든다. 보존 정리가 동작하는지 보려고."""
    backend = open_backend("local-primary")
    now = utcnow()
    for service, item, days in (("orders", "database", (12, 10, 9)),
                                ("billing", "database", (8, 6, 5))):
        for d in days:
            backup_id = f"{service}-{stamp(now - timedelta(days=d))}"
            target = backend.path(f"{service}/{item}/{backup_id}")
            target.mkdir(parents=True, exist_ok=True)
            (target / f"{service}.db").write_text("(오래된 백업)", encoding="utf-8")


def seed_unregistered() -> None:
    """정책 밖에서 도는 작업이 남긴 산출물."""
    backend = open_backend("local-primary")
    for path in (f"shipping/database/shipping-{stamp(utcnow())}",
                 f"orders/legacy-dump/orders-{stamp(utcnow())}"):
        target = backend.path(path)
        target.mkdir(parents=True, exist_ok=True)
        (target / "dump.bin").write_text("(정책에 없는 백업)", encoding="utf-8")


def count_backups(service: str, item: str) -> int:
    return len(open_backend("local-primary").list_dirs(f"{service}/{item}"))


# ── 시나리오 ─────────────────────────────────────────────────────────

def main() -> int:
    print("정책 기반 백업 틀 — 데모")
    seed()
    seed_old_backups()

    title(1, "정책 검사와 실행 생성")
    note("CI 가 merge 전에 하는 일. 판정마다 필요한 항목이 다르다.")
    cmd_lint(None)
    note("정책에서 어떤 실행이 생기는지. judgement: backup 인 항목만 스케줄이 생긴다.")
    cmd_render(None)

    title(2, "orders 백업 — 정상")
    note("데이터와 설정이 하나의 backup_id 로 묶인다. 함께 복원해야 짝이 맞기 때문이다.")
    before = count_backups("orders", "database")
    result = run_backup("orders", "database")
    show(result)
    after = count_backups("orders", "database")
    note(f"보존 정리 — 백업 {before}개 → {after}개. "
         f"삭제 {len(result.retention.get('deleted', []))}건. "
         f"기록을 남긴 뒤에 삭제했다.")

    title(3, "billing 백업 — 원본이 옮겨간 뒤 정책이 따라가지 못한 경우")
    note("정책은 billing-legacy.db 를 가리키는데 실제 서비스는 billing.db 를 쓴다.")
    before = count_backups("billing", "database")
    run_backup("billing", "database")          # 어제치
    result = run_backup("billing", "database")  # 오늘치
    show(result)
    after = count_backups("billing", "database")
    note(f"B1 에서 막혔으므로 어댑터를 호출하지 않았다.")
    note(f"보존 정리도 하지 않았다 — 기존 백업 {before}개 → {after}개 그대로.")
    note("삭제를 먼저 하는 구현이라면 여기서 백업은 없고 삭제만 완료됐을 것이다.")

    title(4, "orders 복구 시험")
    note("백업 직전에 저장해 둔 기준값과 복구본을 대조한다.")
    note("작업 성공과 복구 가능은 별개이므로, 여기를 통과해야 '검증됨' 이 된다.")
    result = run_restore_test("orders", "database")
    show(result)

    title(5, "정책 밖에서 도는 작업")
    note("목록에 없는 백업이 저장소에 쌓이고 있는 상황을 만든다.")
    seed_unregistered()
    note("shipping/database 와 orders/legacy-dump 산출물을 넣었다. 둘 다 정책에 없다.")

    title(6, "점검 — 안 되고 있는 것")
    note("정책(있어야 할 것)과 기록(실제 있었던 것)을 맞댄 결과.")
    cmd_check(None)

    gaps = compute_gaps()
    kinds = sorted({g["kind"] for g in gaps})
    print(f"\n{'━' * 72}")
    print(f"  드러난 공백 유형 {len(kinds)}가지 — {', '.join(kinds)}")
    print(f"{'━' * 72}")
    print("\n  이 가운데 어느 것도 사람이 목록을 들여다봐서 찾은 것이 아니다.")
    print("  정책과 실행 기록을 맞대면 저절로 나온다.")
    print("\n  대시보드로 보려면:  python -m backupfw.api")
    return 0


if __name__ == "__main__":
    sys.exit(main())
