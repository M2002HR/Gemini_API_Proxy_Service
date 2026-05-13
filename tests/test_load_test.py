from __future__ import annotations

import importlib.util
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path


def _load_module():
    path = Path(__file__).resolve().parents[1] / "scripts" / "load_test.py"
    spec = importlib.util.spec_from_file_location("load_test_script", path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)  # type: ignore[arg-type]
    return mod


def test_prompt_length_respects_min_max() -> None:
    mod = _load_module()
    cfg = mod.LoadTestConfig(
        payload={
            "prompt_templates": ["short"],
            "prompt_min_chars": 60,
            "prompt_max_chars": 80,
            "random_suffix_chars": 0,
            "include_request_metadata_in_prompt": False,
        }
    )
    runner = mod.LoadRunner(cfg)
    _, prompt = runner._build_prompt(seq=1)
    assert len(prompt) >= 60
    assert len(prompt) <= 80


def test_schedule_offset_with_ramp_up_monotonic() -> None:
    mod = _load_module()
    cfg = mod.LoadTestConfig(
        load_profile={
            "total_requests": 20,
            "requests_per_second": 10.0,
            "concurrency": 5,
            "ramp_up_seconds": 5.0,
        }
    )
    runner = mod.LoadRunner(cfg)
    offsets = [runner._schedule_offset(i) for i in range(20)]
    assert all(offsets[i] < offsets[i + 1] for i in range(len(offsets) - 1))
    assert offsets[0] > 0


def test_build_admin_delta_tracks_incidents_and_runtime_deltas() -> None:
    mod = _load_module()
    run_start = datetime.now(timezone.utc)

    before = {
        "rotation_state": {
            "mode": "cloudflare_worker",
            "active_key_slot": 1,
            "active_worker_slot": 1,
            "keys": [
                {"slot": 1, "key_mask": "k1", "runtime": {"success_count": 10, "failure_count": 2}},
            ],
            "workers": [
                {"slot": 1, "worker_url": "https://w1", "runtime": {"success_count": 20, "failure_count": 4}},
            ],
        },
        "incidents": {"total": 5, "items": []},
    }
    after = {
        "rotation_state": {
            "mode": "cloudflare_worker",
            "active_key_slot": 1,
            "active_worker_slot": 1,
            "keys": [
                {"slot": 1, "key_mask": "k1", "runtime": {"success_count": 15, "failure_count": 3}},
            ],
            "workers": [
                {"slot": 1, "worker_url": "https://w1", "runtime": {"success_count": 31, "failure_count": 5}},
            ],
        },
        "incidents": {
            "total": 8,
            "items": [
                {"ts": (run_start + timedelta(seconds=1)).isoformat(), "kind": "rate_limited"},
                {"ts": (run_start + timedelta(seconds=2)).isoformat(), "kind": "retry_on_5xx"},
            ],
        },
        "usage_recent": {"requests_in_window": 40},
    }

    delta = mod.build_admin_delta(before=before, after=after, run_started_at=run_start.isoformat())
    assert delta["available"] is True
    assert delta["incidents"]["total_delta"] == 3
    assert delta["incidents"]["new_incidents_by_kind"]["rate_limited"] == 1
    assert delta["rotation_state"]["workers"]["total_success_delta"] == 11
    assert delta["rotation_state"]["keys"]["total_failure_delta"] == 1


def test_build_failure_analysis_infers_location_and_cause() -> None:
    mod = _load_module()
    run_start = datetime.now(timezone.utc)
    rec = mod.RequestRecord(
        seq=1,
        request_id="req-1",
        scheduled_offset_sec=0.1,
        started_at_iso=run_start.isoformat(),
        ended_at_iso=(run_start + timedelta(seconds=1)).isoformat(),
        latency_ms=1000.0,
        status_code=429,
        status_class="4xx",
        success=False,
        error_type="http_429",
        error_detail="rate limited",
        error_status="RESOURCE_EXHAUSTED",
        error_code=429,
        error_message="Resource exhausted",
        response_request_id="req-1",
        model="gemini-2.5-flash",
        endpoint="proxy_gemini",
        prompt_len=100,
        template_index=0,
        response_bytes=100,
        response_content_type="application/json",
        proxy_metadata={"served_via": "cloudflare_worker", "key_slot": 1},
        usage_metadata={},
    )

    admin_after = {
        "incidents": {
            "items": [
                {
                    "ts": (run_start + timedelta(seconds=2)).isoformat(),
                    "kind": "rate_limited",
                    "context": {"request_id": "req-1"},
                }
            ]
        }
    }
    out = mod.build_failure_analysis(
        records=[rec],
        admin_after=admin_after,
        run_started_at=run_start.isoformat(),
        top_n_signatures=5,
    )

    assert out["total_failures"] == 1
    assert out["by_location"]["worker_path_or_upstream_gemini"] == 1
    assert out["by_cause"]["upstream_rate_limited"] == 1
    assert out["correlated_incident_kinds"]["rate_limited"] == 1
