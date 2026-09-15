#!/usr/bin/env python3
"""sqlite 어댑터.

제품별 백업 지식은 이 파일 안에만 있다. 실행기는 이 파일이 무엇을 하는지 모르고,
아래 계약만 지키면 다른 어댑터로 교체할 수 있다.

    adapter <verb> --input request.json --output manifest.json

    verb   capabilities | baseline | backup | verify-artifact
           restore | verify-data | delete
    exit   0 성공  1 실패  2 사전조건 미충족  3 부분 성공(재시도 금지)
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

VERSION = "1.0.0"

# 어댑터가 위 계층에 자기 능력을 말하는 유일한 통로.
# 실행기는 어댑터 이름이 아니라 이 값으로 분기한다.
CAPABILITIES = {
    "adapter": "sqlite",
    "version": VERSION,
    "consistency": ["online-snapshot"],
    "pitr": False,                       # 특정 시점 복구 불가 → rpo 가 주기보다 짧으면 실행기가 차단
    "needs": {"filesystem": ["source"]},
    "restore_requirements": {"engine": "sqlite3"},
    "supports_verify": ["integrity-check", "sql"],
    "estimated_duration_per_gb": "PT1M",
}


class Precondition(Exception):
    """사전 조건 미충족. 재시도해도 소용없다 → exit 2"""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def source_path(req: dict) -> Path:
    locator = req["source"]["locator"]
    if not locator.startswith("file://"):
        raise Precondition(f"지원하지 않는 locator 형식: {locator}")
    return Path(locator[len("file://"):])


def measure(db_path: Path, specs: list) -> dict:
    """정책의 verify.data 명세를 실제 값으로 바꾼다.

    백업 직전(baseline)과 복구 후(verify-data)에 같은 명세를 돌려 대조한다.
    """
    result = {}
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        for spec in specs or []:
            if spec.get("builtin") == "integrity-check":
                result["integrity-check"] = con.execute("PRAGMA integrity_check").fetchone()[0]
            elif "sql" in spec:
                result[spec["sql"]] = con.execute(spec["sql"]).fetchone()[0]
    finally:
        con.close()
    return result


# ── verbs ────────────────────────────────────────────────────────────

def v_capabilities(_req: dict) -> dict:
    return CAPABILITIES


def v_baseline(req: dict) -> dict:
    src = source_path(req)
    if not src.exists():
        raise Precondition(f"원본이 없다: {src}")
    return {"baseline": measure(src, req.get("verify", []))}


def v_backup(req: dict) -> dict:
    src = source_path(req)
    if not src.exists():
        raise Precondition(f"원본이 없다: {src}")

    target = Path(req["target"]["path"])
    target.mkdir(parents=True, exist_ok=True)
    dst = target / src.name

    # sqlite 온라인 백업 API. 쓰기가 진행 중이어도 일관된 사본을 만든다.
    # 실행 중인 데이터 디렉터리를 그냥 복사하는 것과 다른 지점이다.
    s = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
    d = sqlite3.connect(dst)
    try:
        s.backup(d)
    finally:
        d.close()
        s.close()

    return {
        "artifacts": [{
            "file": src.name,
            "size": dst.stat().st_size,
            "sha256": sha256(dst),
            "data_time": now_iso(),
        }],
        "tool": f"sqlite3 {sqlite3.sqlite_version}",
        "consistency": "online-snapshot",
    }


def v_verify_artifact(req: dict) -> dict:
    """산출물이 정말 그 형식인지 확인한다. 크기와 해시만으로는 알 수 없는 것."""
    target = Path(req["target"]["path"])
    checked = []
    for f in sorted(target.glob("*.db")):
        con = sqlite3.connect(f"file:{f}?mode=ro", uri=True)
        try:
            verdict = con.execute("PRAGMA integrity_check").fetchone()[0]
        finally:
            con.close()
        if verdict != "ok":
            raise RuntimeError(f"{f.name} 무결성 검사 실패: {verdict}")
        checked.append(f.name)
    if not checked:
        raise RuntimeError("검사할 산출물이 없다")
    return {"verified": checked}


def v_restore(req: dict) -> dict:
    artifact_dir = Path(req["artifact"]["path"])
    target = Path(req["target"]["path"])
    target.mkdir(parents=True, exist_ok=True)
    restored = []
    for f in sorted(artifact_dir.glob("*.db")):
        shutil.copy2(f, target / f.name)
        restored.append(f.name)
    if not restored:
        raise RuntimeError(f"복원할 파일이 없다: {artifact_dir}")
    return {"restored": restored, "engine": f"sqlite3 {sqlite3.sqlite_version}"}


def v_verify_data(req: dict) -> dict:
    """복구된 사본에 baseline 과 같은 명세를 돌린다. 값 비교는 실행기가 한다."""
    target = Path(req["target"]["path"])
    files = sorted(target.glob("*.db"))
    if not files:
        raise RuntimeError(f"복원된 파일이 없다: {target}")
    return {"measured": measure(files[0], req.get("verify", []))}


def v_delete(req: dict) -> dict:
    target = Path(req["target"]["path"])
    if target.exists():
        shutil.rmtree(target)
    return {"deleted": str(target)}


VERBS = {
    "capabilities": v_capabilities,
    "baseline": v_baseline,
    "backup": v_backup,
    "verify-artifact": v_verify_artifact,
    "restore": v_restore,
    "verify-data": v_verify_data,
    "delete": v_delete,
}


def main() -> int:
    ap = argparse.ArgumentParser(description="sqlite 백업 어댑터")
    ap.add_argument("verb", choices=sorted(VERBS))
    ap.add_argument("--input")
    ap.add_argument("--output", required=True)
    args = ap.parse_args()

    req = {}
    if args.input:
        req = json.loads(Path(args.input).read_text(encoding="utf-8"))

    try:
        out = VERBS[args.verb](req)
    except Precondition as e:
        print(f"[precondition] {e}", file=sys.stderr)
        return 2
    except Exception as e:  # noqa: BLE001 - 어떤 실패든 계약대로 1 로 돌려준다
        print(f"[failed] {type(e).__name__}: {e}", file=sys.stderr)
        return 1

    Path(args.output).write_text(
        json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
