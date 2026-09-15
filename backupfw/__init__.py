"""정책 기반 백업 틀 — 최소 구현.

계층
    정책    policies/<service>.yaml      무엇을 왜 언제 지킬지 (담당자가 씀)
    실행    backupfw/runner.py           순서 · 판정 · 기록 (B1~B6)
    어댑터  adapters/*.py                제품별 명령
    저장소  backupfw/backends.py         위치. 정책은 이름만 안다
"""

__version__ = "0.1.0"
