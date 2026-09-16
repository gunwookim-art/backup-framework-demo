# 쿠버네티스에 올리기

`k8s/` 아래 매니페스트로 이 틀을 실제 클러스터에서 돌린다.
검증은 k3s 3노드(server 1 + agent 2)에서 했다.

```
policies/orders.yaml ──helm values──▶ CronJob backup-orders-database
                                          │ python -m backupfw backup orders database
                                          ▼
                          /app/demo-data (원본)  ──어댑터──▶  /store (PVC)
                                                                 │
                                          /store/offsite/results ─┘
                                                     │
                                          python -m backupfw.api  ─▶  /gaps
```

## 구성

| 파일 | 역할 |
| --- | --- |
| `Dockerfile` | runner 이미지. `backupfw` + `adapters` 를 한 컨테이너에 담는다 |
| `k8s/manifests/00-base.yaml` | 네임스페이스 · ServiceAccount · PVC · `storage.yaml` ConfigMap |
| `k8s/manifests/10-seed.yaml` | 백업 대상 원본을 PVC 에 만든다 |
| `k8s/manifests/20-api.yaml` | 조회 API |
| `k8s/charts/backup-runner/` | 정책 → CronJob |
| `k8s/applicationset.yaml` | 정책 파일 하나 = Application 하나 (Argo CD) |
| `k8s/deploy.sh` | 위를 순서대로 실행 |

## 배포

```bash
git clone https://github.com/gunwookim-art/backup-framework-demo
cd backup-framework-demo
bash k8s/deploy.sh
```

`deploy.sh` 가 하는 일은 네 가지다.

1. `podman build` 로 runner 이미지를 만든다
2. `k3s ctr images import` 로 세 노드에 넣는다 — 레지스트리를 쓰지 않는다
3. 기반 매니페스트와 시드 Job 을 적용한다
4. `helm template | kubectl apply` 로 정책마다 CronJob 을 만든다

Argo CD 로 가려면 4번 대신 `kubectl apply -f k8s/applicationset.yaml` 을 쓴다.
기반 매니페스트(1~3)는 ApplicationSet 이 만들지 않으므로 먼저 적용해야 한다.

`applicationset.yaml` 은 `main` 을 가리킨다. 다른 브랜치에서 시험하려면 `revision` 과
`targetRevision` 을 그 브랜치로 바꿔서 적용한다.

## 확인

```bash
# 판정이 backup 인 항목만 스케줄이 생겼는가
kubectl -n backup-system get cronjob

# 정상 백업
kubectl -n backup-system create job --from=cronjob/backup-orders-database orders-1
kubectl -n backup-system logs job/orders-1

# 원본이 어긋난 서비스 — blocked 로 끝나고 기존 백업이 남아야 한다
kubectl -n backup-system create job --from=cronjob/backup-billing-database billing-1
kubectl -n backup-system logs job/billing-1

# 안 되고 있는 것
kubectl -n backup-system port-forward deploy/backup-api 8080:8080
curl -s localhost:8080/gaps
```

`legacy-reports` 는 모든 항목이 `hold` · `exclude` 라 CronJob 이 하나도 생기지 않는다.
`orders/config` 는 `schedule: with:database` 라 별도 CronJob 없이 `database` 실행에 묶인다.

## 저장소를 PVC 로 옮긴 방법

코드는 고치지 않았다. `storage.yaml` 의 `root` 를 절대경로로 준 것이 전부다.

```yaml
backends:
  local-primary: {type: local, root: /store/primary}
  local-offsite: {type: local, root: /store/offsite}
```

`LocalBackend.__init__` 이 `ROOT / cfg["root"]` 로 붙이는데, `pathlib` 은 오른쪽이
절대경로면 왼쪽을 버린다. 그래서 `local` 드라이버가 그대로 PVC 를 가리킨다.

이 방식은 `root` 에만 통한다. **원본 locator 는 절대경로로 쓸 수 없다** — 아래 참고.

## 볼륨 배치

| 컨테이너 경로 | 무엇 | 왜 그 자리인가 |
| --- | --- | --- |
| `/app` | 이미지 (`backupfw` · `adapters` · `policies`) | `config.ROOT` 가 여기다 |
| `/app/policies` | ConfigMap — 이 서비스의 정책 | `load_policy()` 가 `ROOT/policies/<service>.yaml` 을 읽는다 |
| `/app/storage.yaml` | ConfigMap — 저장소 설정 | 이미지의 것을 덮어쓴다 |
| `/app/demo-data` | PVC — 백업 대상 원본 | locator 가 `file://demo-data/...` 라 `ROOT` 밑이어야 한다 |
| `/store` | PVC — 산출물 + 결과 기록 | `storage.yaml` 의 절대경로 |

`demo-state/{tmp,staging,restore}` 는 이미지 안에 그대로 둔다. 실행 중에만 쓰는
자리라 파드와 함께 사라져도 된다.

---

# 클러스터에서 확인한 것

k3s 3노드(server 1 + agent 2, Rocky 9.8)에서 실제로 돌렸다.

| 확인 | 결과 |
| --- | --- |
| 판정이 `backup` 인 항목만 스케줄이 생긴다 | CronJob 3개. `legacy-reports` 는 0개, `orders/config` 는 `database` 에 묶여 별도 생성 없음 |
| 정상 백업 | `orders` success — B1~B6 통과, 산출물 `/store/primary`, 결과 `/store/offsite` |
| 어긋난 원본 | `billing` blocked — B1 에서 멈추고 `B6 실패·차단이므로 보존 정리를 하지 않는다` |
| **보존 정리가 건너뛰어지는가** | 오래된 백업 3건을 심어 두고 차단을 유발 → **3건 그대로 남음** |
| 복구 시험 | success — 격리 복원 후 기준값 2건·1건 일치, `latest-verified.json` 생성 |
| 조회 | `/gaps` 5건 · `/services` 판정 분포 정상 |
| ApplicationSet | 정책 3개 → Application 3개 자동 생성, 전부 Synced/Healthy |
| 정책 추가 | `policies/shipping.yaml` 추가 → 20초 내 `backup-shipping` 생성 (hold 뿐이라 CronJob 은 안 생김) |
| 정책 삭제 | 파일 제거 → Application 과 ConfigMap 이 함께 prune, `미등록 서비스` 공백 복귀 |

보존 정리 확인이 이 중 제일 중요하다. README 가 말하는 "흔한 구현은 삭제를 먼저 하기
때문에 백업이 실패하면 삭제만 완료된다"의 반대가 실제로 성립하는 것을 본 것이다.

## 전제 — Argo CD 설치 상태

ApplicationSet 을 쓰려면 `applicationsets.argoproj.io` CRD 가 있어야 한다.

`kubectl apply -f install.yaml` 로 Argo CD 를 깔면 이 CRD 가 **조용히 빠지는 일이 있다.**
CRD 가 커서 `kubectl` 이 붙이는 `last-applied-configuration` 어노테이션이 256KB 한도를
넘기 때문이다. 이때 `argocd-applicationset-controller` 가 CrashLoopBackOff 로 남는다.

```bash
kubectl get crd | grep applicationsets   # 없으면 아래
kubectl apply --server-side --force-conflicts -n argocd \
  -f https://raw.githubusercontent.com/argoproj/argo-cd/stable/manifests/install.yaml
```

# 돌려보며 발견한 것

## 고친 것

**차트의 CronJob 인자가 CLI 계약과 맞지 않았다.**
`args: ["--service=orders", "--item=database"]` 였는데 `backupfw.cli` 는 위치 인자를
받는다(`backup <service> <item>`). 서브커맨드 자체도 빠져 있었다. 이대로면 파드가
`argparse` 오류로 즉시 죽는다. `args: ["backup", "orders", "database"]` 로 고쳤다.

**정책 ConfigMap 의 마운트 위치와 키 이름이 코드와 어긋났다.**
`/policy/policy.yaml` 로 마운트했는데 `load_policy()` 는 `ROOT/policies/<service>.yaml`
을 찾는다. 키를 `<service>.yaml` 로 바꾸고 `/app/policies` 에 마운트해서, 정책을 읽는
코드가 컨테이너인지 아닌지 몰라도 되게 했다.

**정책 ConfigMap 에 차트 동작용 값이 섞여 들어갔다.**
`{{ toYaml .Values }}` 가 `global`(이미지 이름 등)까지 정책 본문에 넣었다. 정책이 아닌
것이 정책으로 기록된다. `omit .Values "global"` 로 뺐다.

**ApplicationSet 의 경로 두 곳이 틀렸다.**
`path: charts/backup-runner` 는 실제로 `k8s/charts/backup-runner` 이고, `repoURL` 은
`example` 플레이스홀더였다. `valueFiles` 상대 경로도 차트가 두 단계가 아니라 세 단계
아래라 `../../../` 여야 한다.

**`policies/_template.yaml` 이 ApplicationSet 에 잡힌다.**
`service` 가 비어 있어 이름이 `backup-` 인 Application 이 생긴다. `exclude: true` 로 뺐다.

## 고치지 않고 남긴 것

**차단된 실행을 쿠버네티스가 재시도한다.** 클러스터에서 처음 드러난 것이다.

`cmd_backup` 은 `result.status != "success"` 면 `1` 을 돌려준다. `blocked` 도 여기
해당하므로 Job 이 실패로 끝나고, `backoffLimit: 2` 에 따라 **3번 실행된다.** 그때마다
결과 기록이 하나씩 쌓인다. `billing` 을 두 번 돌린 뒤 결과 파일이 6개가 됐고
`/gaps` 에 `실패 지속 — 최근 5회 연속 실패` 가 올라왔다.

실제로 일어난 일은 **정책과 원본이 어긋났다는 하나의 사실**이다. 재시도해도 결과가
달라질 수 없고(경로가 틀린 것이다), 기록만 늘어나 현황을 부풀린다.

어댑터 계약에는 이미 이 구분이 있다 — `exit 2` 는 사전 조건 미충족이라 재시도하지
않는다. 실행기 수준에는 그 대응물이 없다. 고르는 방법은 두 가지다.

1. `blocked` 일 때 `0` 을 돌려준다. 차단은 실행기가 **제대로 판단해서 멈춘 것**이고
   결과도 기록됐으므로 프로세스 실패가 아니라고 보는 쪽
2. CronJob 의 `backoffLimit` 을 `0` 으로 둔다. 다만 진짜 일시적 실패도 재시도되지 않는다

1번이 계약에 더 맞아 보이지만 `blocked` 를 성공으로 셀지는 이 저장소의 판단이라
건드리지 않았다.

**`activeDeadlineSeconds` 가 rto 의 2배로 계산된다.**
`mul (regexFind "[0-9]+" .rto | int) 7200` 이라 `rto: 2h` 가 4시간이 된다. 시간 단위를
초로 바꾸는 것이라면 `3600` 이어야 한다. 여유를 의도한 것일 수 있어 값은 두고 주석에
"그 2배"라고 적어 두었다. 또한 `regexFind` 가 첫 숫자만 집으므로 `90m` 은 90시간의
2배가 된다 — 단위를 무시한다.

**조회 API 가 루프백에만 바인딩한다.**
`api.py:115` 의 `ThreadingHTTPServer(("127.0.0.1", port))` 때문에 Service 를 만들어도
닿지 않는다. 그래서 `20-api.yaml` 에 Service 를 두지 않고 `port-forward` 로 확인하게
했다. 클러스터 안에서 노출하려면 바인드 주소가 설정 가능해야 하는데, 이건 이 저장소의
설계 판단이라 건드리지 않았다.

**`backup-verify` 네임스페이스를 실제로 쓸 수 없다.**
복구 시험을 격리하려고 만들었지만, 저장소가 RWO PVC 라 네임스페이스를 넘지 못한다.
지금 격리는 컨테이너 안 `demo-state/restore/<run_id>` 디렉터리 수준이다. 네임스페이스
단위로 격리하려면 RWX 저장소가 먼저 필요하다.

## 물어본 것에 대한 답

**`backends.py` 의 `local` 드라이버는 PVC 와 어떻게 맞물리는가**

잘 맞는다. 코드 변경이 필요 없었다. `root` 를 절대경로로 주면 그대로 PVC 를 가리킨다.

다만 **원본 locator 에는 같은 수가 통하지 않는다.** `runner.probe_source` 가

```python
path = ROOT / locator[len("file://"):]
...
"resolved": str(path.relative_to(ROOT))
```

를 하는데, locator 가 절대경로면 `path` 가 `ROOT` 밖으로 나가 `relative_to` 가
`ValueError` 를 던진다. 즉 **산출물 저장소는 어디든 둘 수 있지만 백업 대상은 반드시
`ROOT` 아래여야 한다.** 그래서 PVC 를 `/app/demo-data` 에 마운트했다.

이건 계약이 어색한 지점이다. 실제 환경에서 백업 대상은 `/var/lib/postgresql` 처럼
절대경로에 있는 것이 보통이고, 그때마다 `ROOT` 아래로 마운트를 끌어오는 것은 부자연
스럽다. `resolved` 를 `relative_to` 대신 `os.path.relpath` 나 절대경로 그대로 기록하면
풀린다.

**`results.backend` 를 클러스터 밖에 둘 수 있는가**

지금 구조로는 안 된다. `local` 드라이버가 로컬 파일 경로만 알기 때문에, 밖에 두려면
둘 중 하나다.

1. 밖에 있는 저장소를 **여전히 로컬 경로처럼 보이게** 한다 — NFS/CIFS 를 RWX PV 로
   마운트하고 `root` 를 그 경로로 준다. 코드 변경 없음
2. `s3` 드라이버를 구현한다. `storage.yaml` 에 이미 `type: s3` 자리가 있고
   `DRIVERS` 에 한 줄 추가하는 구조라, **정책 파일은 그대로 두고** 바꿀 수 있다

설계 의도(정책은 저장소 이름만 안다)는 여기서 실제로 지켜진다. 1번은 오늘 당장 되고,
2번은 `ResultsStore` 가 쓰는 메서드(`write_json` · `read_json` · `glob_json`)만 맞추면
된다. 다만 `glob_json` 이 `Path.glob` 을 돌려주는 시그니처라, 객체 저장소 드라이버를
붙이려면 이 반환형을 먼저 추상화해야 한다.

**결정 11(runner·어댑터 배치)에 대한 관찰**

지금은 한 컨테이너가 맞다. `call_adapter` 가 `sys.executable` 로 같은 파일시스템의
`adapters/*.py` 를 실행하고, 요청·응답을 `demo-state/tmp` 의 파일로 주고받기 때문이다.
sidecar 나 별도 Job 으로 쪼개려면 그 파일 교환이 볼륨 공유나 네트워크 호출로 바뀌어야
하고, 그건 어댑터 계약(`--input` / `--output` 파일 경로) 자체의 변경이다.

어댑터가 제품 CLI(`pg_dump` 등)를 요구하기 시작하면 한 이미지에 전부 넣는 방식이
먼저 무너진다. 그때가 결정 11 을 다시 볼 시점이다.
