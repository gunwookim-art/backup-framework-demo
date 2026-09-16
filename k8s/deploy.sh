#!/usr/bin/env bash
# gw-k3s01 에서 실행. 이미지 빌드 → 노드 배포 → 매니페스트 적용 → CronJob 생성.
#
#   bash k8s/deploy.sh            전체
#   bash k8s/deploy.sh image      이미지만 다시 빌드·배포
#   bash k8s/deploy.sh apply      매니페스트·차트만 다시 적용
#
# 전제
#   - 저장소 루트에서 실행
#   - k3s server 노드 (sudo k3s ctr 사용)
#   - podman 또는 docker
#   - agent 노드로 이미지를 밀 때만 ssh 필요 (포트 6879)
set -euo pipefail

IMAGE=backup-runner:0.1.0
TAR=/tmp/backup-runner.tar
NS=backup-system
AGENTS="${AGENTS:-10.10.200.237 10.10.200.238}"
SSH_PORT="${SSH_PORT:-6879}"
STEP="${1:-all}"

say() { printf '\n\033[1m== %s\033[0m\n' "$*"; }

builder() {
    if command -v podman >/dev/null 2>&1; then echo podman
    elif command -v docker >/dev/null 2>&1; then echo docker
    else
        echo "podman 또는 docker 가 필요하다: sudo dnf -y install podman" >&2
        exit 1
    fi
}

do_image() {
    local B; B=$(builder)
    say "이미지 빌드 ($B)"
    "$B" build -t "$IMAGE" .

    say "이미지 내보내기"
    rm -f "$TAR"
    if [ "$B" = podman ]; then
        "$B" save --format docker-archive -o "$TAR" "$IMAGE"
    else
        "$B" save -o "$TAR" "$IMAGE"
    fi
    ls -lh "$TAR"

    say "server 노드에 import"
    sudo k3s ctr images import "$TAR"

    # PVC 가 local-path 라 파드는 한 노드에 묶이지만, 어느 노드가 될지는 첫 스케줄이
    # 정한다. 세 노드 모두에 넣어 두면 그 선택과 무관하게 돈다.
    for ip in $AGENTS; do
        say "agent $ip 에 import"
        if scp -P "$SSH_PORT" -o StrictHostKeyChecking=accept-new "$TAR" "$ip:/tmp/" >/dev/null; then
            ssh -p "$SSH_PORT" "$ip" "sudo k3s ctr images import $TAR && rm -f $TAR"
        else
            echo "  !! $ip 전송 실패 — 그 노드에 스케줄되면 ImagePullBackOff 가 난다" >&2
        fi
    done
}

do_apply() {
    say "기반 매니페스트 (네임스페이스 · SA · PVC · storage)"
    kubectl apply -f k8s/manifests/00-base.yaml

    say "원본 데이터 시드"
    kubectl -n "$NS" delete job backup-seed --ignore-not-found
    kubectl apply -f k8s/manifests/10-seed.yaml
    kubectl -n "$NS" wait --for=condition=complete job/backup-seed --timeout=180s
    kubectl -n "$NS" logs job/backup-seed

    say "정책 → CronJob (helm template | kubectl apply)"
    # ApplicationSet 없이 먼저 확인한다. Argo CD 경로는 k8s/applicationset.yaml.
    for p in policies/*.yaml; do
        case "$(basename "$p")" in _*) continue ;; esac
        svc=$(basename "$p" .yaml)
        echo "  -- $svc"
        helm template "backup-$svc" k8s/charts/backup-runner -n "$NS" -f "$p" \
            | kubectl apply -n "$NS" -f -
    done

    say "조회 API"
    kubectl apply -f k8s/manifests/20-api.yaml
    kubectl -n "$NS" rollout status deploy/backup-api --timeout=180s
}

case "$STEP" in
    image) do_image ;;
    apply) do_apply ;;
    all)   do_image; do_apply ;;
    *)     echo "사용법: bash k8s/deploy.sh [all|image|apply]" >&2; exit 1 ;;
esac

say "현재 상태"
kubectl -n "$NS" get cronjob,job,pod,pvc

cat <<'EOF'

다음 확인

  # 1. CronJob 목록 — legacy-reports 는 없어야 한다
  kubectl -n backup-system get cronjob

  # 2. orders 백업 수동 실행 → success
  kubectl -n backup-system create job --from=cronjob/backup-orders-database orders-manual-1
  kubectl -n backup-system logs job/orders-manual-1

  # 3. billing 백업 수동 실행 → blocked, 기존 백업 유지
  kubectl -n backup-system create job --from=cronjob/backup-billing-database billing-manual-1
  kubectl -n backup-system logs job/billing-manual-1

  # 4. 공백 목록
  kubectl -n backup-system port-forward deploy/backup-api 8080:8080
  curl -s localhost:8080/gaps
EOF
