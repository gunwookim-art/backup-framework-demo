"""실행기 — 백업 1회의 순서 · 판정 · 기록.

실제 백업은 어댑터가 한다. 이 파일은 그 앞뒤만 지킨다.

    B1  정책과 실제 원본 대조 · 어댑터 능력 확인 · 용량 확인
        어긋나면 blocked 으로 기록하고 종료. 어댑터 호출도 보존 정리도 하지 않는다.
    B2  검증 기준값 수집 — 복구 후 대조할 값을 지금 저장한다
    B3  어댑터 호출
    B4  산출물 검증 — 존재 · 크기 0 아님 · 크기 일치 · 검증값 일치
                      · 기준 시점 · 형식 유효 · 직전 대비 급감 아님
    B5  함께 복원할 항목을 하나의 backup_id 로 묶기
    B6  결과 기록 → 지표 → 그 다음에야 보존 정리

어댑터 이름으로 분기하지 않는다. capabilities 로만 분기한다.
이 규칙이 깨지면 어댑터는 교체 가능한 부품이 아니게 된다.
"""
from __future__ import annotations

import hashlib
import json
import os
import secrets
import shutil
import subprocess
import sys
from pathlib import Path

from .backends import (backends_for, open_backend, parse_locator,
                       results_store)
from .config import ROOT, parse_stamp, stamp, utcnow
from .policy import Item, Policy, check_objectives, load_policy
from .result import Blocked, Result

TMP = ROOT / "demo-state" / "tmp"
STAGING = ROOT / "demo-state" / "staging"
RESTORE = ROOT / "demo-state" / "restore"


# ── 어댑터 호출 ───────────────────────────────────────────────────────

def _adapter_script(item: Item) -> Path:
    name = str(item.adapter or "").split("@")[0].replace("-", "_")
    path = ROOT / "adapters" / f"{name}.py"
    if not path.exists():
        raise Blocked(f"어댑터를 찾을 수 없다: {item.adapter}")
    return path


def _tail(text: str, n: int = 400) -> str:
    return (text or "").strip()[-n:]


def call_adapter(item: Item, verb: str, payload: dict, run_id: str = "x") -> dict:
    """계약: adapter <verb> --input req.json --output manifest.json

    exit 2 는 사전 조건 미충족이므로 재시도하지 않고 Blocked 로 올린다.
    """
    script = _adapter_script(item)
    TMP.mkdir(parents=True, exist_ok=True)
    tag = f"{run_id}-{item.name}-{verb}"
    req_path = TMP / f"{tag}.req.json"
    out_path = TMP / f"{tag}.out.json"

    req = {"run_id": run_id, "verb": verb, **payload}
    req_path.write_text(json.dumps(req, ensure_ascii=False, indent=2), encoding="utf-8")

    env = dict(os.environ, PYTHONUTF8="1", PYTHONIOENCODING="utf-8")
    proc = subprocess.run(
        [sys.executable, str(script), verb,
         "--input", str(req_path), "--output", str(out_path)],
        cwd=str(ROOT), capture_output=True, text=True,
        encoding="utf-8", errors="replace", env=env, timeout=600,
    )
    if proc.returncode == 2:
        raise Blocked(_tail(proc.stderr) or "사전 조건 미충족")
    if proc.returncode != 0:
        raise RuntimeError(
            f"{item.adapter} {verb} 실패 (exit {proc.returncode}): {_tail(proc.stderr)}")
    if not out_path.exists():
        raise RuntimeError(f"{item.adapter} {verb} 가 결과를 남기지 않았다")
    return json.loads(out_path.read_text(encoding="utf-8"))


# ── B1 ───────────────────────────────────────────────────────────────

def _dir_size(path: Path) -> int:
    return sum(f.stat().st_size for f in path.rglob("*") if f.is_file())


def probe_source(item: Item, result: Result) -> int:
    """정책이 가리키는 원본이 실제로 거기 있는지 확인하고, 본 것을 기록한다.

    서버 이전이나 경로 변경 뒤에 백업 설정만 남는 상태를 여기서 잡는다.
    """
    locator = item.source.get("locator")
    if not locator:
        raise Blocked(f"{item.name}: source.locator 가 없다")
    if not locator.startswith("file://"):
        raise Blocked(f"{item.name}: 이 데모가 다루지 않는 locator 형식 {locator}")

    path = ROOT / locator[len("file://"):]
    if not path.exists():
        raise Blocked(f"{item.name}: 정책이 가리키는 원본이 실제로 없다 — {locator}")

    size = path.stat().st_size if path.is_file() else _dir_size(path)
    result.observed.setdefault("source", {})[item.name] = {
        "locator": locator,
        "resolved": str(path.relative_to(ROOT)).replace("\\", "/"),
        "bytes": size,
    }
    return size


def check_capacity(item: Item, need: int, result: Result) -> None:
    names, _ = backends_for(item)
    for name in names:
        backend = open_backend(name)
        free = backend.free_bytes()
        if free < need * 2 + (1 << 20):
            raise Blocked(f"저장소 {name} 여유 공간 부족 ({free}B)")
        result.observed.setdefault("backends", {})[name] = {
            "fault_domain": backend.fault_domain,
            "free_bytes": free,
        }


def b1(item: Item, result: Result) -> dict:
    caps = call_adapter(item, "capabilities", {}, result.run_id)
    result.observed.setdefault("adapter", {})[item.name] = \
        f"{caps.get('adapter')}@{caps.get('version')}"
    result.observed.setdefault("consistency", {})[item.name] = caps.get("consistency")

    problem = check_objectives(item, caps)
    if problem:
        raise Blocked(f"{item.name}: {problem}")

    size = probe_source(item, result)
    check_capacity(item, size, result)
    return caps


# ── B4 ───────────────────────────────────────────────────────────────

def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def _previous_total(store, service: str, item_name: str) -> int:
    latest = store.read_latest(service, item_name)
    if not latest:
        return 0
    return sum(a.get("size", 0) for a in latest.get("artifacts", [])
               if a.get("item") == item_name)


def b4(item: Item, manifest: dict, staging: Path, result: Result, store) -> int:
    """어댑터가 한 말을 믿지 않고 직접 확인한다."""
    artifacts = manifest.get("artifacts") or []
    if not artifacts:
        raise RuntimeError(f"{item.name}: 어댑터가 산출물을 보고하지 않았다")

    total = 0
    for a in artifacts:
        f = staging / a.get("file", "")
        if not f.exists():
            raise RuntimeError(f"{item.name}: 산출물이 없다 — {a.get('file')}")
        actual = f.stat().st_size
        if actual == 0:
            raise RuntimeError(f"{item.name}: 산출물 크기가 0 이다 — {a.get('file')}")
        if actual != a.get("size"):
            raise RuntimeError(
                f"{item.name}: 크기 불일치 — 실제 {actual}, 보고 {a.get('size')}")
        if sha256_file(f) != a.get("sha256"):
            raise RuntimeError(f"{item.name}: 검증값 불일치 — {a.get('file')}")
        if not a.get("data_time"):
            raise RuntimeError(f"{item.name}: 데이터 기준 시점이 없다")
        total += actual

    # 파일이 정말 그 형식인지는 어댑터만 안다
    call_adapter(item, "verify-artifact",
                 {"target": {"path": str(staging)}}, result.run_id)

    previous = _previous_total(store, result.service, item.name)
    if previous and total < previous * 0.5:
        raise RuntimeError(
            f"{item.name}: 산출물 크기 급감 — 직전 {previous}B → 이번 {total}B")

    return total


# ── 저장 확정 ────────────────────────────────────────────────────────

def commit(item: Item, staging: Path, backup_id: str) -> list[str]:
    """검증을 통과한 것만 최종 위치로 옮긴다. 저장소 종류는 어댑터가 몰랐다."""
    names, path = backends_for(item)
    locators = []
    for name in names:
        backend = open_backend(name)
        locators.append(backend.put_dir(staging, f"{path}/{backup_id}"))
    return locators


# ── B6 보존 정리 ─────────────────────────────────────────────────────

def apply_retention(policy: Policy, members: list[Item], store) -> dict:
    """성공한 실행에서만 부른다.

    흔한 구현은 이 단계를 백업보다 먼저 한다. 그러면 백업이 실패했을 때
    삭제만 완료된 상태로 끝난다.
    """
    locks = set(store.read_locks(policy.service))
    deleted, kept, protected = [], 0, []
    now = utcnow()

    for item in members:
        rules = item.retention or {}
        keep_days = rules.get("keep_days")
        if keep_days is None:
            continue
        min_copies = int(rules.get("min_copies", 1))
        names, path = backends_for(item)

        for name in names:
            backend = open_backend(name)
            entries = backend.list_dirs(path)
            survivors = len(entries)
            for backup_id in sorted(entries):
                when = parse_stamp(backup_id.split("-")[-1])
                if when is None:
                    kept += 1
                    continue
                age_days = (now - when).days
                if age_days <= keep_days:
                    kept += 1
                    continue
                if backup_id in locks:
                    protected.append(backup_id)      # 복구 시험이 쓰는 중
                    kept += 1
                    continue
                if survivors - 1 < min_copies:
                    kept += 1
                    continue
                backend.delete(f"{path}/{backup_id}")
                deleted.append(f"{name}:{backup_id}")
                survivors -= 1

    return {"deleted": deleted, "kept": kept, "locked": protected}


# ── 백업 실행 ────────────────────────────────────────────────────────

def run_backup(service: str, leader: str, policy_version: str = "local") -> Result:
    policy = load_policy(service)
    members = policy.members(leader)
    store = results_store()

    started = utcnow()
    run_id = f"{service}-{leader}-{stamp(started)}-{secrets.token_hex(2)}"
    result = Result(run_id=run_id, kind="backup", service=service, item=leader,
                    policy_version=policy_version, owner_role=policy.owner_role)
    result.started_at = started
    result.status = "running"
    result.objectives = dict(policy.item(leader).objectives)

    # ① 시작 기록. 끝에만 쓰면 프로세스가 강제 종료됐을 때
    #    '실행하지 않음' 과 '실행하다 죽음' 이 구분되지 않는다.
    store.write(result)

    staging_root = STAGING / run_id
    try:
        backup_id = f"{service}-{stamp(started)}"
        result.backup_id = backup_id

        for item in members:
            b1(item, result)
            result.step(f"B1:{item.name}", "ok", "정책과 원본 일치")

            base = call_adapter(item, "baseline",
                                {"source": item.source, "verify": item.verify_data},
                                run_id)
            result.baseline[item.name] = base.get("baseline", {})
            result.step(f"B2:{item.name}", "ok", f"기준값 {len(result.baseline[item.name])}건")

            staging = staging_root / item.name
            staging.mkdir(parents=True, exist_ok=True)
            manifest = call_adapter(item, "backup",
                                    {"source": item.source,
                                     "target": {"path": str(staging)}},
                                    run_id)
            result.step(f"B3:{item.name}", "ok", manifest.get("tool"))

            total = b4(item, manifest, staging, result, store)
            result.step(f"B4:{item.name}", "ok", f"{total}B 검증 통과")

            locators = commit(item, staging, backup_id)
            for a in manifest["artifacts"]:
                result.artifacts.append({
                    **a,
                    "item": item.name,
                    "locators": locators,
                    "tool": manifest.get("tool"),
                    "consistency": manifest.get("consistency"),
                })

        result.verification["artifact"] = "pass"
        result.step("B5", "ok", f"{len(members)}개 항목을 {backup_id} 로 묶음")
        result.status = "success"

    except Blocked as e:
        result.status = "blocked"
        result.error = str(e)
        result.step("B1", "blocked", str(e))
    except Exception as e:  # noqa: BLE001
        result.status = "failed"
        result.error = f"{type(e).__name__}: {e}"
        result.step("B3/B4", "failed", result.error)
    finally:
        result.ended_at = utcnow()
        shutil.rmtree(staging_root, ignore_errors=True)

        if result.status == "success":
            # B6 — 기록이 먼저, 삭제는 그 다음
            result.retention = apply_retention(policy, members, store)
            result.step("B6", "ok",
                        f"기록 후 보존 정리 — {len(result.retention['deleted'])}건 삭제")
        else:
            result.step("B6", "skipped", "실패·차단이므로 보존 정리를 하지 않는다")

        store.write(result)
        if result.status == "success":
            store.write_pointer(result)

    return result


# ── 복구 시험 ────────────────────────────────────────────────────────

def run_restore_test(service: str, leader: str) -> Result:
    policy = load_policy(service)
    members = policy.members(leader)
    store = results_store()

    started = utcnow()
    run_id = f"{service}-{leader}-restore-{stamp(started)}-{secrets.token_hex(2)}"
    result = Result(run_id=run_id, kind="restore_test", service=service, item=leader,
                    owner_role=policy.owner_role)
    result.started_at = started
    result.status = "running"
    store.write(result)

    source_run = store.read_latest(service, leader)
    restore_root = RESTORE / run_id
    backup_id = None

    try:
        if not source_run or source_run.get("status") != "success":
            raise Blocked("복구 시험에 쓸 성공한 백업이 없다")

        backup_id = source_run["backup_id"]
        store.lock(service, backup_id)          # 보존 정리가 지우지 못하게
        result.backup_id = backup_id
        result.step("R1", "ok", f"{backup_id} 선택 · 보존 정리에서 제외")

        # 운영과 격리된 곳에만 복원한다
        restore_root.mkdir(parents=True, exist_ok=True)
        result.step("R2", "ok", f"격리 위치 {restore_root.relative_to(ROOT)}")

        all_pass = True
        for item in members:
            locators = []
            for a in source_run.get("artifacts", []):
                if a.get("item") == item.name:
                    locators = a.get("locators", [])
                    break
            if not locators:
                raise RuntimeError(f"{item.name}: 복원할 산출물 기록이 없다")

            name, key = parse_locator(locators[0])
            artifact_dir = open_backend(name).path(key)
            target = restore_root / item.name

            call_adapter(item, "restore",
                         {"artifact": {"path": str(artifact_dir)},
                          "target": {"path": str(target)}}, run_id)
            result.step(f"R4:{item.name}", "ok", "복원 완료")

            measured = call_adapter(item, "verify-data",
                                    {"target": {"path": str(target)},
                                     "verify": item.verify_data}, run_id)
            got = measured.get("measured", {})
            expected = source_run.get("baseline", {}).get(item.name, {})
            result.measured[item.name] = got

            mismatches = {k: (expected.get(k), got.get(k))
                          for k in expected if expected.get(k) != got.get(k)}
            if mismatches:
                all_pass = False
                result.step(f"R5:{item.name}", "failed", f"기준값 불일치 {mismatches}")
            else:
                result.step(f"R5:{item.name}", "ok",
                            f"기준값 {len(expected)}건 일치")

        result.verification["data"] = "pass" if all_pass else "fail"
        result.verification["service"] = "not_run"   # 데모에는 서비스 기동이 없다
        result.status = "success" if all_pass else "failed"
        if not all_pass:
            result.error = "복구본이 백업 시점 기준값과 다르다"

    except Blocked as e:
        result.status, result.error = "blocked", str(e)
        result.step("R1", "blocked", str(e))
    except Exception as e:  # noqa: BLE001
        result.status = "failed"
        result.error = f"{type(e).__name__}: {e}"
        result.step("R4/R5", "failed", result.error)
    finally:
        result.ended_at = utcnow()
        if backup_id:
            store.unlock(service, backup_id)
        shutil.rmtree(restore_root, ignore_errors=True)
        result.step("R6", "ok", "임시 자원 정리 · 보존 잠금 해제")
        store.write(result)

        # 검증 결과를 원래 백업 기록에도 반영한다.
        # 하나의 backup_id 에 백업 실행과 검증 실행 두 줄이 붙는다.
        if result.verification["data"] == "pass" and source_run:
            source_run["verification"]["data"] = "pass"
            key = f"{store.prefix}/{service}/{leader}"
            store.backend.write_json(f"{key}/latest.json", source_run)
            store.backend.write_json(f"{key}/latest-verified.json", source_run)

    return result
