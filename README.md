# backup-framework-demo

정책 파일 하나로 백업이 실행되고, 검증되고, **안 되고 있는 것이 저절로 드러나는** 백업 틀의 최소 구현.

백업 도구가 아니다. 서비스마다 다른 백업 도구를 그대로 두고 **보호 대상 정의 · 실행 흐름 · 결과 형식 · 판정 기준**만 같은 규격으로 맞추는 틀이다.

```
정책 파일 (YAML)  ──▶  실행기(runner)  ──▶  어댑터  ──▶  저장소
      │                     │                              │
      │                     └── 실행 결과(JSON) ───────────┤
      └──────────────── 조회 API ◀───────────────────────┘
                              │
                     "안 되고 있는 것" 목록
```

## 왜

백업 작업이 등록되어 있다는 것과 복구할 수 있다는 것은 별개다. 흔히 이런 일이 벌어진다.

- 백업 작업은 매일 성공으로 끝나는데 산출물이 0바이트다
- 원본 서버 주소가 바뀌었는데 백업 설정은 그대로여서 몇 주째 빈 디렉터리만 쌓인다
- 보존 기간 초과분 삭제가 백업보다 **먼저** 실행되어, 백업이 실패하면 삭제만 완료된다
- 담당자가 퇴사한 뒤 아무도 그 서비스의 백업을 보지 않는다
- 목록에 없는 백업이 어딘가에서 돌다가 조용히 멈춘다

공통점은 **어긋났다는 사실이 드러나지 않는다**는 것이다. 이 저장소는 그 어긋남이 드러나게 만드는 최소 구조를 보여준다.

## 5분 데모

```bash
pip install -r requirements.txt
python demo.py            # 시나리오 6개 실행
python -m backupfw.api    # http://localhost:8080 대시보드
```

`demo.py`가 순서대로 보여주는 것:

| # | 시나리오 | 확인되는 것 |
| --- | --- | --- |
| 1 | 정책 → 실행 렌더링 | 정책의 `judgement: backup` 항목만 스케줄이 생긴다 |
| 2 | `orders` 정상 백업 | 데이터와 설정이 하나의 `backup_id`로 묶인다 |
| 3 | `billing` 백업 | 정책의 원본과 실제가 달라 **B1에서 차단**되고, **옛 백업은 지워지지 않는다** |
| 4 | 보존 정리 | 성공한 실행에서만 초과분이 삭제된다 |
| 5 | 복구 시험 | 격리 복원 후 백업 직전에 저장해 둔 기준값과 대조한다 |
| 6 | 점검 | 8가지 공백이 목록으로 나온다 |

## 구조

### 네 계층

| 계층 | 이 저장소에서 | 바뀌는 시점 |
| --- | --- | --- |
| 정책 | `policies/<service>.yaml` | 서비스 추가, 정책 개정 |
| 실행 | `backupfw/runner.py` — B1~B6 순서·판정·기록 | 실행 도구 교체 |
| 어댑터 | `adapters/*.py` — 제품별 명령 | 새 기술 추가 |
| 저장소 | `backupfw/backends.py` + `storage.yaml` | 저장소 전환 |

### 세 가지 규칙

1. **정책이 없는 실행은 없다.** 스케줄은 정책에서만 생성된다. 정책 밖에서 도는 작업은 점검이 "미등록 실행"으로 잡는다.
2. **결과는 정책 키로 연결된다.** 모든 실행 결과는 `{service, item, policy_version}`을 갖는다. 정책 저장소에 결과를 쓰지 않는다.
3. **계층은 자기 일만 안다.** 어댑터는 보존 삭제를 하지 않고, 실행기는 제품 명령을 모르고, 저장소는 서비스 이름을 해석하지 않는다.

## 정책 파일

서비스마다 하나. 담당자가 쓴다.

```yaml
service: orders
owner_role: team-orders          # 개인 이름이 아니라 역할
items:
  - name: database
    kind: data                   # data | config | log
    judgement: backup            # backup | regenerate | exclude | hold
    reason: "주문 원장. 재생성 불가"
    source:
      type: sqlite
      locator: "file://demo-data/orders.db"
    adapter: sqlite@1
    schedule: "0 3 * * *"
    retention: {keep_days: 7, min_copies: 1}
    target: "backend://local-primary/orders/database"
    objectives: {rpo: 24h, rto: 2h}
    verify:
      data:
        - builtin: integrity-check
        - sql: "select count(*) from orders"
    restore_test: {schedule: "0 5 * * 1"}
```

### 판정이 넷인 이유

"백업 안 함"을 한 단어로 적으면 **다시 만들 수 있어서인지, 판단을 못 해서인지** 구분되지 않는다. 그래서 넷으로 나눈다.

| 판정 | 뜻 | 추가로 적어야 하는 것 |
| --- | --- | --- |
| `backup` | 잃으면 다시 만들 수 없음 | 어댑터 · 주기 · 보존 · 저장소 |
| `regenerate` | 원천에서 다시 만들 수 있음 | 재생성 절차와 소요 시간 |
| `exclude` | 보호 불필요 | 사유 |
| `hold` | 판단할 정보가 부족함 | 사유 · 재검토 기한 · 담당 역할 |

`hold`에 기한을 강제하기 때문에 "나중에 정하자"가 무기한으로 남지 않는다. 기한이 지나면 조회 화면에 나온다.

## 실행기가 하는 일

실행기는 **백업을 하지 않는다.** 순서를 지키고 판정하고 기록한다.

```
B1  정책과 실제 원본 대조 · 어댑터 능력 확인 · 용량 확인
    → 어긋나면 blocked 으로 기록하고 종료. 어댑터 호출도 보존 삭제도 하지 않음
B2  검증 기준값 수집 (복구 후 대조할 값을 지금 저장)
B3  어댑터 호출
B4  산출물 검증 — 존재 · 크기 0 아님 · 크기 일치 · 검증값 일치 · 기준 시점 · 형식 유효 · 급감 아님
B5  함께 복원할 항목을 하나의 backup_id 로 묶기
B6  결과 기록 → 지표 → 그 다음에야 보존 정리
```

`B6`의 순서가 핵심이다. 흔한 구현은 보존 삭제를 먼저 하기 때문에, 백업이 실패하면 삭제만 완료된 상태로 끝난다.

실행기는 **시작할 때도 결과를 쓴다.** 끝에만 쓰면 프로세스가 강제 종료됐을 때 기록이 남지 않아 "실행하지 않음"과 "실행하다 죽음"이 구분되지 않는다.

## 어댑터 계약

모든 어댑터가 같은 인터페이스를 따른다. 제품 지식은 여기에만 있다.

```
adapter <verb> --input request.json --output manifest.json

verb   capabilities | backup | restore | verify-artifact | delete
exit   0 성공  1 실패  2 사전조건 미충족  3 부분 성공(재시도 금지)
```

`capabilities`는 어댑터가 위 계층에 말하는 유일한 통로다.

```json
{ "adapter": "sqlite", "version": "1.0.0",
  "consistency": ["online-snapshot"],
  "pitr": false,
  "restore_requirements": { "engine": "sqlite3" },
  "supports_verify": ["integrity-check", "sql"] }
```

정책이 `rpo`를 백업 주기보다 짧게 잡았는데 어댑터가 `pitr: false`면 실행기가 차단한다.

**실행기에 어댑터 이름으로 분기하는 코드를 두지 않는다.** `if adapter == "sqlite"`가 아니라 `if caps["pitr"]`로 분기한다. 이 규칙이 깨지면 어댑터는 더 이상 교체 가능한 부품이 아니다.

## 저장소

정책은 저장소의 **이름만** 안다.

```yaml
# storage.yaml
backends:
  local-primary: { type: local, root: demo-state/artifacts/primary,  fault_domain: host-a }
  local-offsite: { type: local, root: demo-state/artifacts/offsite,  fault_domain: host-b }
results:
  backend: local-offsite        # 결과 기록은 한 곳으로 고정
```

- **산출물 저장소는 여러 개**여도 된다. `min_copies: 2`면 두 곳에 쓴다.
- **결과 기록 저장소는 하나로 고정**한다. 흩어지면 전체 현황을 만들 때 모든 저장소를 뒤져야 하고, 한 곳이 죽으면 현황 자체가 불완전해진다.
- 결과는 **산출물과 다른 곳**에 둔다. 산출물 저장소가 사라지면 "무엇이 있었는지"까지 함께 사라지기 때문이다.

저장소를 바꿀 때 고치는 것은 `storage.yaml` 한 줄과 데이터 이동뿐이다. 정책 파일은 건드리지 않는다.

## 조회

```
GET /services          서비스별 항목 수 · 판정 분포 · 최근 상태 · 담당 역할
GET /services/{svc}    항목별 정책 · 최근 실행 · 검증 결과
GET /runs/{run_id}     단계별 결과
GET /gaps              안 되고 있는 것
```

`/gaps`는 **정책(있어야 할 것)** 과 **결과 기록(실제 있었던 것)** 을 맞대어 계산한다.

| 상태 | 계산 |
| --- | --- |
| 미실행 | 마지막 성공이 예정 간격의 1.5배를 넘음 |
| 실패 지속 | 최근 연속 실패 |
| 산출물 이상 | 크기가 0이거나 직전 대비 급감 |
| 미검증 | 데이터 검증을 통과한 백업이 없음 |
| 미등록 실행 | 저장소에 있는데 대응하는 정책이 없음 |
| 미등록 서비스 | 목록에 있는데 정책 파일이 없음 |
| 미승계 | 담당 역할에 배정된 사람이 0명 |
| 재검토 초과 | `hold` 인데 기한이 지남 |

## 쿠버네티스에서

`k8s/`에 참고용 매니페스트가 있다. 핵심은 **정책 파일이 곧 Helm 차트의 values**라는 점이다.

```
policies/orders.yaml  ──ApplicationSet──▶  Application backup-orders
                                                │ helm template
                                                ▼
                                    judgement: backup 항목마다 CronJob
```

- 정책 파일이 있으면 Application이 있고, 없으면 없다 → **미등록 서비스가 배포 화면에서도 보인다**
- CI는 merge 전 검사만 한다. 렌더 결과를 Git에 되쓰지 않는다
- 생성된 CronJob에는 `backup.example.io/policy` 라벨이 붙는다. 이 라벨이 없는 작업은 점검이 "미등록 실행"으로 잡는다

## 이 저장소가 다루지 않는 것

- 실제 운영 복구 실행. 복구 시험은 격리 환경에서만 한다
- 자격증명 보관. 정책에는 **참조만** 적고 값은 시크릿 관리자에서 온다
- 대규모 데이터. 어댑터가 감당할 수 있는 범위를 넘는 대상은 별도 설계가 필요하다

## 라이선스

MIT
