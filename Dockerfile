# runner 이미지 — backupfw + adapters 를 한 컨테이너에 담는다.
#
# 결정 11(runner·어댑터 배치)은 아직 미결이다. 지금은 가장 단순한 쪽을 골랐다:
# 어댑터가 `sys.executable` 로 호출되는 같은 프로세스 트리이므로 한 컨테이너가 맞다.
# sidecar 나 별도 Job 으로 가려면 call_adapter 의 subprocess 호출부터 바뀐다.
#
# 어댑터가 sqlite3 · tarfile 등 표준 라이브러리만 쓰므로 외부 바이너리가 없다.
# 새 어댑터가 제품 CLI(pg_dump 등)를 요구하면 그때 이 이미지에 추가한다.

FROM python:3.12-slim

# /app 이 backupfw.config.ROOT 가 된다. 정책·어댑터·storage.yaml 경로가 전부 여기 기준.
WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY backupfw/ ./backupfw/
COPY adapters/ ./adapters/
COPY policies/ ./policies/
COPY storage.yaml roles.yaml inventory.yaml ./

# 실행 중에만 쓰는 작업 디렉터리. 컨테이너 수명과 함께 사라져도 되는 것들이다.
#  - demo-state/tmp      어댑터 요청·응답 JSON
#  - demo-state/staging  검증 전 산출물
#  - demo-state/restore  복구 시험 격리 공간
# 산출물과 결과 기록은 여기가 아니라 PVC(/store)로 간다.
RUN mkdir -p demo-state/tmp demo-state/staging demo-state/restore demo-data

# 비루트 실행. PVC 쓰기는 파드의 securityContext.fsGroup 으로 맞춘다.
RUN useradd --uid 10001 --create-home --shell /usr/sbin/nologin runner \
    && chown -R 10001:10001 /app
USER 10001

ENV PYTHONUNBUFFERED=1 \
    PYTHONUTF8=1 \
    PYTHONIOENCODING=utf-8 \
    PYTHONDONTWRITEBYTECODE=1

# `python -m backupfw <verb> ...` — CLI 가 그대로 진입점이다.
#   backup <svc> <item> · restore-test <svc> <item> · check · lint · render · services
ENTRYPOINT ["python", "-m", "backupfw"]
CMD ["check"]
