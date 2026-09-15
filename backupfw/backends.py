"""저장소 드라이버와 결과 기록소.

정책은 저장소의 '이름'만 안다 — backend://local-primary/orders/database
이름을 실제 위치로 바꾸는 일은 여기서만 한다. 어댑터는 로컬 경로만 받는다.

실행기(쓰기)와 조회 API(읽기)가 이 모듈을 함께 쓴다.
실행기는 실행 중에만 살아 있으므로 무언가를 '제공'하지 않는다. 쓰는 쪽일 뿐이다.
"""
from __future__ import annotations

import json
import shutil
from pathlib import Path

from .config import (ROOT, backend_config, results_backend_name,
                     results_prefix, utcnow)


def parse_locator(locator: str) -> tuple[str, str]:
    """'backend://local-primary/orders/database' → ('local-primary', 'orders/database')"""
    if not locator or not locator.startswith("backend://"):
        raise ValueError(f"backend:// 형식이 아니다: {locator}")
    rest = locator[len("backend://"):]
    name, _, path = rest.partition("/")
    return name, path.strip("/")


class LocalBackend:
    """디렉터리 기반 저장소. 실제 환경의 NFS 마운트나 객체 저장소 자리."""

    def __init__(self, name: str, cfg: dict):
        self.name = name
        self.cfg = cfg
        self.root = ROOT / cfg["root"]

    def __repr__(self) -> str:
        return f"<LocalBackend {self.name} {self.cfg.get('root')}>"

    @property
    def fault_domain(self) -> str | None:
        return self.cfg.get("fault_domain")

    def path(self, key: str) -> Path:
        return self.root / key

    def put_dir(self, src: Path, key: str) -> str:
        """검증을 통과한 산출물을 최종 위치로 확정한다."""
        dst = self.path(key)
        dst.parent.mkdir(parents=True, exist_ok=True)
        if dst.exists():
            shutil.rmtree(dst)
        shutil.copytree(src, dst)
        return f"backend://{self.name}/{key}"

    def list_dirs(self, key: str) -> list[str]:
        base = self.path(key)
        if not base.is_dir():
            return []
        return sorted(p.name for p in base.iterdir() if p.is_dir())

    def delete(self, key: str) -> None:
        target = self.path(key)
        if target.exists():
            shutil.rmtree(target)

    def free_bytes(self) -> int:
        self.root.mkdir(parents=True, exist_ok=True)
        return shutil.disk_usage(self.root).free

    # 결과 기록용 ------------------------------------------------------
    def write_json(self, key: str, obj) -> None:
        p = self.path(key)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")

    def read_json(self, key: str):
        p = self.path(key)
        if not p.exists():
            return None
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return None

    def glob_json(self, pattern: str) -> list[Path]:
        return sorted(self.root.glob(pattern))


DRIVERS = {"local": LocalBackend}


def open_backend(name: str) -> LocalBackend:
    cfg = backend_config(name)
    kind = cfg.get("type", "local")
    if kind not in DRIVERS:
        raise NotImplementedError(
            f"'{kind}' 드라이버는 이 데모에 없다. backends.py 에 추가하면 "
            f"정책 파일은 그대로 두고 저장소만 바뀐다."
        )
    return DRIVERS[kind](name, cfg)


def backends_for(item) -> tuple[list[str], str]:
    """정책의 target 과 min_copies 로 쓸 저장소들을 정한다."""
    primary, path = parse_locator(item.target)
    names = [primary]
    want = int((item.retention or {}).get("min_copies", 1))
    if want > 1:
        for other in _enabled_local_names():
            if other != primary and len(names) < want:
                names.append(other)
    return names, path


def _enabled_local_names() -> list[str]:
    from .config import storage
    return [n for n, c in storage().get("backends", {}).items()
            if c.get("enabled", True) and c.get("type", "local") == "local"]


class ResultsStore:
    """실행 결과 기록소.

    반드시 한 곳이다. 흩어지면 전체 현황을 만들 때 모든 저장소를 뒤져야 하고,
    한 곳이 죽으면 현황 자체가 불완전해진다.
    산출물 저장소와 분리한다 — 산출물이 사라져도 '무엇이 있었는지'는 남아야 한다.
    """

    def __init__(self):
        self.backend = open_backend(results_backend_name())
        self.prefix = results_prefix()

    def _run_key(self, service: str, item: str, run_id: str) -> str:
        return f"{self.prefix}/{service}/{item}/{run_id}.json"

    def write(self, result) -> None:
        self.backend.write_json(
            self._run_key(result.service, result.item, result.run_id),
            result.to_dict(),
        )

    def write_pointer(self, result) -> None:
        base = f"{self.prefix}/{result.service}/{result.item}"
        self.backend.write_json(f"{base}/latest.json", result.to_dict())
        if result.verification.get("data") == "pass":
            self.backend.write_json(f"{base}/latest-verified.json", result.to_dict())

    def read_latest(self, service: str, item: str):
        return self.backend.read_json(f"{self.prefix}/{service}/{item}/latest.json")

    def read_latest_verified(self, service: str, item: str):
        return self.backend.read_json(
            f"{self.prefix}/{service}/{item}/latest-verified.json")

    def list_runs(self, service: str | None = None, item: str | None = None) -> list[dict]:
        pattern = f"{self.prefix}/{service or '*'}/{item or '*'}/*.json"
        runs = []
        for p in self.backend.glob_json(pattern):
            if p.name.startswith("latest"):
                continue
            data = self.backend.read_json(str(p.relative_to(self.backend.root)).replace("\\", "/"))
            if data:
                runs.append(data)
        runs.sort(key=lambda r: r.get("started_at") or "", reverse=True)
        return runs

    def get_run(self, run_id: str):
        for r in self.list_runs():
            if r.get("run_id") == run_id:
                return r
        return None

    # 복구 시험 중인 백업을 보존 정리가 지우지 않도록 ---------------------
    def _lock_key(self, service: str) -> str:
        return f"{self.prefix}/_locks/{service}.json"

    def read_locks(self, service: str) -> list[str]:
        return self.backend.read_json(self._lock_key(service)) or []

    def lock(self, service: str, backup_id: str) -> None:
        held = set(self.read_locks(service))
        held.add(backup_id)
        self.backend.write_json(self._lock_key(service), sorted(held))

    def unlock(self, service: str, backup_id: str) -> None:
        held = [b for b in self.read_locks(service) if b != backup_id]
        self.backend.write_json(self._lock_key(service), held)


_store: ResultsStore | None = None


def results_store() -> ResultsStore:
    global _store
    if _store is None:
        _store = ResultsStore()
    return _store


def artifact_backend_names() -> list[str]:
    """산출물이 놓일 수 있는 저장소들. 미등록 실행을 찾을 때 훑는다."""
    return _enabled_local_names()


def scan_artifact_tree(name: str) -> dict[str, list[str]]:
    """저장소에 실제로 존재하는 <service>/<item> 목록."""
    b = open_backend(name)
    found: dict[str, list[str]] = {}
    if not b.root.is_dir():
        return found
    for svc_dir in sorted(p for p in b.root.iterdir() if p.is_dir()):
        if svc_dir.name.startswith("_") or svc_dir.name == results_prefix():
            continue
        items = sorted(p.name for p in svc_dir.iterdir() if p.is_dir())
        if items:
            found[svc_dir.name] = items
    return found
