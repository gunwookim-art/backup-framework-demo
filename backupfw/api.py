"""조회 API.

입력은 셋뿐이다.
    정책 (policies/*.yaml)   있어야 할 것
    결과 기록 (results/)      실제 있었던 것
    저장소 스캔               정책 밖에서 도는 것

세 가지를 맞대어 '안 되고 있는 것' 을 만든다.
실행기와는 분리한다 — 실행기가 멈춰도 현황은 보여야 하고,
조회가 멈춰도 백업은 돌아야 한다.

    python -m backupfw.api          http://localhost:8080
"""
from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from .backends import results_store
from .checker import compute_gaps, summarize
from .policy import load_all_policies

DASHBOARD = Path(__file__).with_name("dashboard.html")


def api_services() -> dict:
    return summarize()


def api_service(name: str) -> dict:
    store = results_store()
    for policy in load_all_policies():
        if policy.service != name:
            continue
        items = []
        for item in policy.items:
            latest = store.read_latest(policy.service, item.name) if item.is_leader else None
            items.append({
                "name": item.name,
                "kind": item.kind,
                "judgement": item.judgement,
                "reason": item.reason,
                "adapter": item.adapter,
                "schedule": item.schedule,
                "target": item.target,
                "objectives": item.objectives,
                "review_by": str(item.review_by) if item.review_by else None,
                "latest": latest,
            })
        return {"service": policy.service, "owner_role": policy.owner_role,
                "tier": policy.tier, "items": items}
    return {"error": f"정책이 없는 서비스: {name}"}


def api_runs(limit: int = 50) -> dict:
    return {"runs": results_store().list_runs()[:limit]}


def api_run(run_id: str) -> dict:
    run = results_store().get_run(run_id)
    return run or {"error": f"없는 실행: {run_id}"}


def api_gaps() -> dict:
    gaps = compute_gaps()
    by_kind: dict[str, int] = {}
    for gap in gaps:
        by_kind[gap["kind"]] = by_kind.get(gap["kind"], 0) + 1
    return {"count": len(gaps), "by_kind": by_kind, "gaps": gaps}


class Handler(BaseHTTPRequestHandler):
    server_version = "backupfw/0.1"

    def log_message(self, fmt, *args):   # 조용히
        pass

    def _send(self, body: bytes, content_type: str, status: int = 200) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, status: int = 200) -> None:
        self._send(json.dumps(obj, ensure_ascii=False, indent=2).encode("utf-8"),
                   "application/json; charset=utf-8", status)

    def do_GET(self) -> None:  # noqa: N802
        path = urlparse(self.path).path.rstrip("/") or "/"
        try:
            if path == "/":
                self._send(DASHBOARD.read_bytes(), "text/html; charset=utf-8")
            elif path == "/services":
                self._json(api_services())
            elif path.startswith("/services/"):
                self._json(api_service(path.split("/", 2)[2]))
            elif path == "/runs":
                self._json(api_runs())
            elif path.startswith("/runs/"):
                self._json(api_run(path.split("/", 2)[2]))
            elif path == "/gaps":
                self._json(api_gaps())
            else:
                self._json({"error": "없는 경로",
                            "paths": ["/", "/services", "/services/{svc}",
                                      "/runs", "/runs/{id}", "/gaps"]}, 404)
        except Exception as e:  # noqa: BLE001
            self._json({"error": f"{type(e).__name__}: {e}"}, 500)


def serve(port: int = 8080) -> None:
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"  http://localhost:{port}    대시보드")
    print(f"  http://localhost:{port}/gaps   안 되고 있는 것 (JSON)")
    print("  Ctrl+C 로 종료")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n종료")
    finally:
        server.server_close()


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description="백업 현황 조회")
    ap.add_argument("--port", type=int, default=8080)
    serve(ap.parse_args().port)
