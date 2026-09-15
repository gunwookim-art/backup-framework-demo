"""저장소 목록 · 역할 · 서비스 목록을 읽는다. 경로 기준점도 여기서 정한다."""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent


def _load(name: str) -> dict:
    path = ROOT / name
    if not path.exists():
        return {}
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


@lru_cache(maxsize=1)
def storage() -> dict:
    return _load("storage.yaml")


@lru_cache(maxsize=1)
def roles() -> dict:
    return _load("roles.yaml").get("roles", {}) or {}


@lru_cache(maxsize=1)
def inventory() -> list:
    return _load("inventory.yaml").get("services", []) or []


def backend_config(name: str) -> dict:
    cfg = storage().get("backends", {}).get(name)
    if cfg is None:
        raise KeyError(f"storage.yaml 에 없는 저장소 이름: {name}")
    return cfg


def enabled_backends() -> list:
    return [n for n, c in storage().get("backends", {}).items()
            if c.get("enabled", True)]


def results_backend_name() -> str:
    return storage().get("results", {}).get("backend", "local-offsite")


def results_prefix() -> str:
    return storage().get("results", {}).get("prefix", "results")


# ── 시간 유틸 ─────────────────────────────────────────────────────────

STAMP = "%Y%m%dT%H%M%SZ"


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def stamp(dt: datetime) -> str:
    return dt.strftime(STAMP)


def parse_stamp(text: str) -> datetime | None:
    try:
        return datetime.strptime(text, STAMP).replace(tzinfo=timezone.utc)
    except ValueError:
        return None


_DURATION = re.compile(r"^\s*(\d+)\s*([smhd])\s*$", re.I)
_UNIT = {"s": "seconds", "m": "minutes", "h": "hours", "d": "days"}


def parse_duration(text) -> timedelta | None:
    """'24h' · '30m' · '7d' → timedelta"""
    if text is None:
        return None
    m = _DURATION.match(str(text))
    if not m:
        return None
    return timedelta(**{_UNIT[m.group(2).lower()]: int(m.group(1))})


def schedule_interval(expr: str | None) -> timedelta | None:
    """cron 식에서 실행 간격을 뽑는다. 데모에 필요한 형태만 다룬다."""
    if not expr or str(expr).startswith("with:"):
        return None
    parts = str(expr).split()
    if len(parts) != 5:
        return timedelta(days=1)
    minute, hour, dom, _mon, dow = parts
    if minute.startswith("*/"):
        return timedelta(minutes=int(minute[2:]))
    if hour.startswith("*/"):
        return timedelta(hours=int(hour[2:]))
    if hour == "*":
        return timedelta(hours=1)
    if dow != "*":
        return timedelta(days=7)
    if dom != "*":
        return timedelta(days=30)
    return timedelta(days=1)
