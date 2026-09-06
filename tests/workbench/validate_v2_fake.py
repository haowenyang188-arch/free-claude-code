"""SOP v2 七段流水线 fake 模式验收:注册 → 起 run → 跑完 → 断言全流程。

用 adapters 的 SopClient 真实走 HTTP(8000),不 mock。
"""

from __future__ import annotations

import sys
import time

sys.path.insert(0, "/home/gnen/free-claude-code")

from adapters.openhands_acp.sop_client import SopApiError, SopClient  # noqa: E402

EXPECTED_STEPS = [
    "plan",
    "plan_review",
    "revise",
    "re_review",
    "output",
    "final_decision",
    "execute",
]


def main() -> int:
    client = SopClient()
    client.login()
    client.register_definition()
    started = client.start_run(
        "fake 验收:实现一个加法函数 add(a,b) 并附测试",
        metadata={"validation": "fake-v2-smoke"},
    )
    run_id = started.get("sop_run_id") or started.get("id") or ""
    print(f"run started: {run_id}")

    deadline = time.time() + 1800
    status = ""
    dispatched: list[str] = []
    while time.time() < deadline:
        run = client.get_run(run_id)
        status = run.get("status", "")
        if status in ("completed", "failed", "cancelled"):
            break
        if status in ("waiting_review", "running", "paused"):
            handoffs = client._request("GET", f"/api/sop-runs/{run_id}/handoffs")
            pending = [
                h
                for h in handoffs
                if h.get("status") in ("pending", "ready", "waiting")
                and h.get("id") not in dispatched
            ]
            if pending:
                handoff_id = pending[0]["id"]
                try:
                    client._request(
                        "POST",
                        f"/api/sop-runs/{run_id}/handoffs/{handoff_id}/dispatch",
                        {},
                    )
                    dispatched.append(handoff_id)
                    print(f"dispatched handoff {handoff_id[:8]} (#{len(dispatched)})")
                except SopApiError as exc:
                    # 409 = 已被引擎处理;标记跳过即可
                    print(f"dispatch skipped {handoff_id[:8]}: {exc}")
                    dispatched.append(handoff_id)
        time.sleep(1.0)
    print(f"terminal status: {status} (gates dispatched: {len(dispatched)})")

    steps_raw = client._request("GET", f"/api/sop-runs/{run_id}/steps")
    step_map = {s.get("step_id"): s.get("status") for s in steps_raw}
    print("steps:", step_map)

    artifacts = client.get_artifacts(run_id)
    type_role = [(a.get("type"), a.get("role_id")) for a in artifacts]
    print(f"artifacts ({len(artifacts)}):", type_role)

    problems: list[str] = []
    if status != "completed":
        problems.append(f"terminal status = {status} (want completed)")
    for step in EXPECTED_STEPS:
        if step_map.get(step) not in ("completed", "accepted", "done"):
            problems.append(f"step {step} status = {step_map.get(step)}")
    review_count = sum(1 for t, _ in type_role if t == "review_report")
    plan_count = sum(1 for t, _ in type_role if t == "plan")
    impl_count = sum(1 for t, _ in type_role if t in ("implementation", "diff"))
    if review_count < 3:
        problems.append(f"review_report artifacts = {review_count} (want >=3)")
    if plan_count < 2:
        problems.append(f"plan artifacts = {plan_count} (want >=2)")
    if impl_count < 1:
        problems.append(f"implementation artifacts = {impl_count} (want >=1)")

    print("FAKE-V2", "PASS" if not problems else "FAIL: " + "; ".join(problems))
    return 0 if not problems else 1


if __name__ == "__main__":
    sys.exit(main())
