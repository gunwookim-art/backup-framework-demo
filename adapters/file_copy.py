#!/usr/bin/env python3
"""디렉터리 어댑터 — 설정 디렉터리 같은 파일 묶음을 다룬다.

sqlite 어댑터와 하는 일은 전혀 다르지만 계약은 똑같다.
실행기는 두 어댑터를 구분하지 않고 같은 순서로 호출한다.

    adapter <verb> --input request.json --output manifest.json
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
import tarfile
from datetime import datetime, timezone
from pathlib import Path

VERSION = "1.0.0"

CAPABILITIES = {
    "adapter": "file-copy",
    "version": VERSION,
    "consistency": ["archive-snapshot"],
    "pitr": False,
    "needs": {"filesystem": ["source"]},
    "restore_requirements": {"engine": "tar"},
    "supports_verify": ["file-count", "archive-list"],
    "estimated_duration_per_gb": "PT2M",
}

ARCHIVE = "bundle.tar.gz"


class Precondition(Exception):
    """사전 조건 미충족 → exit 2"""


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


def count_files(root: Path) -> int:
    return sum(1 for p in root.rglob("*") if p.is_file())


def measure_dir(root: Path, specs: list) -> dict:
    result = {}
    for spec in specs or []:
        if spec.get("builtin") == "file-count":
            result["file-count"] = count_files(root)
    return result


# ── verbs ────────────────────────────────────────────────────────────

def v_capabilities(_req: dict) -> dict:
    return CAPABILITIES


def v_baseline(req: dict) -> dict:
    src = source_path(req)
    if not src.is_dir():
        raise Precondition(f"원본 디렉터리가 없다: {src}")
    specs = req.get("verify") or [{"builtin": "file-count"}]
    return {"baseline": measure_dir(src, specs)}


def v_backup(req: dict) -> dict:
    src = source_path(req)
    if not src.is_dir():
        raise Precondition(f"원본 디렉터리가 없다: {src}")

    target = Path(req["target"]["path"])
    target.mkdir(parents=True, exist_ok=True)
    archive = target / ARCHIVE

    with tarfile.open(archive, "w:gz") as tar:
        tar.add(src, arcname=src.name)

    return {
        "artifacts": [{
            "file": ARCHIVE,
            "size": archive.stat().st_size,
            "sha256": sha256(archive),
            "data_time": now_iso(),
        }],
        "tool": "tar gz",
        "consistency": "archive-snapshot",
        "entries": count_files(src),
    }


def v_verify_artifact(req: dict) -> dict:
    archive = Path(req["target"]["path"]) / ARCHIVE
    if not archive.exists():
        raise RuntimeError(f"산출물이 없다: {archive}")
    with tarfile.open(archive, "r:gz") as tar:
        names = tar.getnames()
    if not names:
        raise RuntimeError("보관 파일이 비어 있다")
    return {"verified": [ARCHIVE], "entries": len(names)}


def v_restore(req: dict) -> dict:
    archive = Path(req["artifact"]["path"]) / ARCHIVE
    target = Path(req["target"]["path"])
    target.mkdir(parents=True, exist_ok=True)
    if not archive.exists():
        raise RuntimeError(f"복원할 보관 파일이 없다: {archive}")
    with tarfile.open(archive, "r:gz") as tar:
        tar.extractall(target)
    return {"restored": [ARCHIVE]}


def v_verify_data(req: dict) -> dict:
    target = Path(req["target"]["path"])
    roots = [p for p in target.iterdir() if p.is_dir()] if target.exists() else []
    if not roots:
        raise RuntimeError(f"복원된 디렉터리가 없다: {target}")
    specs = req.get("verify") or [{"builtin": "file-count"}]
    return {"measured": measure_dir(roots[0], specs)}


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
    ap = argparse.ArgumentParser(description="디렉터리 백업 어댑터")
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
    except Exception as e:  # noqa: BLE001
        print(f"[failed] {type(e).__name__}: {e}", file=sys.stderr)
        return 1

    Path(args.output).write_text(
        json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
