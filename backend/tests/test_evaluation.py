import asyncio
import json
from pathlib import Path

import httpx
import pytest

from app import evaluation as ev


def records():
    path = ev.metrics_path()
    return (
        [json.loads(line) for line in path.read_text().splitlines()]
        if path.exists()
        else []
    )


def test_usage_aliases_and_missing_usage():
    response = httpx.Response(
        200,
        json={
            "usage": {
                "prompt_tokens": 100,
                "completion_tokens": 25,
                "total_tokens": 125,
                "prompt_tokens_details": {"cached_tokens": 40},
                "secret": "must-not-save",
            },
            "choices": [{"message": {"content": "private response"}}],
        },
    )
    result = ev.response_metrics(response)
    assert result["tokens"] == {"input": 100, "output": 25, "total": 125}
    assert result["usage"]["prompt_tokens_details"]["cached_tokens"] == 40
    assert "secret" not in json.dumps(result) and "private response" not in json.dumps(
        result
    )
    assert ev.response_metrics(httpx.Response(200, json={}))["tokens"] is None
    assert (
        ev.response_metrics(
            httpx.Response(200, json={"usage": {"input_tokens": 0, "output_tokens": 0}})
        )["tokens"]["total"]
        == 0
    )


def test_measured_post_records_failed_usage_without_retry(monkeypatch):
    calls = []

    class Client:
        async def post(self, url, **kwargs):
            calls.append(url)
            return httpx.Response(
                400,
                json={
                    "usage": {"input_tokens": 20, "output_tokens": 0},
                    "error": {"message": "private"},
                },
            )

    with ev.evaluation_context(request_id="request", job_id="job"):
        response = asyncio.run(
            ev.measured_post(
                Client(),
                "https://example.com",
                operation="image_generation",
                provider="meta",
                model="muse-image-1.0",
                json={"prompt": "secret-prompt", "image": "secret-photo"},
                headers={"Authorization": "secret-key"},
            )
        )
    assert response.status_code == 400 and len(calls) == 1
    event = records()[0]
    assert event["outcome"] == "failed" and event["tokens"]["input"] == 20
    assert event["request_id"] == "request" and event["job_id"] == "job"
    assert event["duration_ms"] >= 0
    encoded = json.dumps(event)
    assert all(
        value not in encoded
        for value in ("secret-key", "secret-photo", "secret-prompt", "private")
    )


def test_connect_error_is_recorded_and_propagated():
    class Client:
        calls = 0

        async def post(self, *args, **kwargs):
            self.calls += 1
            raise httpx.ConnectError("connection unavailable")

    client = Client()
    with pytest.raises(httpx.ConnectError):
        asyncio.run(
            ev.measured_post(
                client, "https://example.com", operation="chat", provider="meta"
            )
        )
    assert client.calls == 1
    event = records()[0]
    assert event["outcome"] == "failed" and event["error_type"] == "ConnectError"
    assert event.get("tokens") is None


def test_protocol_error_and_images_are_counted_without_content():
    result = ev.response_metrics(
        httpx.Response(
            200, json={"result": {"structuredContent": {"ucp": {"status": "error"}}}}
        )
    )
    assert result["outcome"] == "failed"
    result = ev.response_metrics(
        httpx.Response(
            200,
            json={
                "data": [{"url": "https://example.com/private"}, {"b64_json": "secret"}]
            },
        ),
        image_response=True,
    )
    assert result["images_returned"] == 2 and "secret" not in json.dumps(result)


def test_nested_contexts_are_isolated_between_async_jobs():
    async def job(identifier):
        with ev.evaluation_context(job_id=identifier):
            await asyncio.sleep(0)
            ev.record_event("test", "success", kind="job")

    async def run():
        with ev.evaluation_context(request_id="shared"):
            await asyncio.gather(job("a"), job("b"))
            assert ev.current_ids() == {"request_id": "shared"}

    asyncio.run(run())
    assert {r["job_id"] for r in records()} == {"a", "b"}
    assert all(r["request_id"] == "shared" for r in records())
    assert ev.current_ids() == {}


def test_api_timing_excludes_background_and_polling(monkeypatch):
    clock = [0.0]
    monkeypatch.setattr(ev.time, "perf_counter", lambda: clock[0])

    async def app(scope, receive, send):
        clock[0] = 0.1
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"{}"})
        clock[0] = 10.0  # Simulate background work after sending the response.

    sent = []

    async def send(message):
        sent.append(message)

    async def receive():
        return {"type": "http.request", "body": b""}

    middleware = ev.EvaluationMiddleware(app)
    asyncio.run(
        middleware(
            {"type": "http", "method": "POST", "path": "/api/search"}, receive, send
        )
    )
    assert records()[0]["duration_ms"] == 100
    assert any(k == b"x-request-id" for k, v in sent[0]["headers"])
    asyncio.run(
        middleware(
            {"type": "http", "method": "GET", "path": "/api/try-on/job"}, receive, send
        )
    )
    assert len(records()) == 1


def test_summary_percentiles_missing_usage_and_no_double_count():
    rows = [
        {
            "kind": "provider_request",
            "operation": "chat",
            "outcome": "success",
            "duration_ms": duration,
            "tokens": {"input": 10, "output": 5, "total": 15},
            "usage": {"total_tokens": 15},
        }
        for duration in (100, 200, 300)
    ]
    rows.append(
        {
            "kind": "provider_request",
            "operation": "chat",
            "outcome": "failed",
            "duration_ms": 400,
        }
    )
    rows.append(
        {
            "kind": "cache",
            "operation": "photo.assessment_cache",
            "outcome": "hit",
            "cache_hit": True,
            "item_count": 2,
        }
    )
    result = ev.summarize(rows)
    chat = next(x for x in result["operations"] if x["operation"] == "chat")
    assert chat["latency_ms"] == {"samples": 4, "mean": 250, "p50": 250, "p95": 385}
    assert chat["tokens"]["total"] == {"sum": 45, "records_reporting": 3}
    assert chat["success_rate"] == 0.75 and chat["usage_records_reported"] == 3
    cache = next(x for x in result["operations"] if x["kind"] == "cache")
    assert cache["cache_hits"] == 2 and cache["tokens"]["total"]["sum"] is None


def test_disabled_and_unwritable_telemetry_do_not_break_app(monkeypatch, tmp_path):
    monkeypatch.setenv("EVALUATION_ENABLED", "0")
    ev.record_event("test", "success")
    assert not records()
    monkeypatch.setenv("EVALUATION_ENABLED", "1")
    monkeypatch.setenv(
        "EVALUATION_FILE", str(tmp_path)
    )  # A directory cannot be appended as a file.
    with ev.measure("test"):
        pass


def test_report_command_filters_runs_and_skips_bad_lines(monkeypatch, tmp_path):
    source = tmp_path / "events.jsonl"
    output = tmp_path / "report.json"
    source.write_text(
        json.dumps(
            {
                "operation": "chat",
                "kind": "provider_request",
                "outcome": "success",
                "run": "live",
            }
        )
        + "\nnot json\n"
        + json.dumps({"operation": "chat", "outcome": "failed", "run": "test"})
        + "\n"
    )
    monkeypatch.setattr(
        "sys.argv",
        [
            "evaluation",
            "--input",
            str(source),
            "--output",
            str(output),
            "--run",
            "live",
        ],
    )
    ev.main()
    report = json.loads(output.read_text())
    assert report["records"] == 1 and report["invalid_lines_skipped"] == 1


def test_department_context_does_not_leak_between_async_jobs():
    async def job(department):
        with ev.evaluation_context(job_id=department, department=department):
            await asyncio.sleep(0)
            ev.record_event("preview.job", "completed", kind="job")

    async def run():
        with ev.evaluation_context(request_id="shared"):
            await asyncio.gather(job("male"), job("female"))
            assert "department" not in ev.current_ids()

    asyncio.run(run())
    assert {(r["job_id"], r["department"]) for r in records()} == {
        ("male", "male"),
        ("female", "female"),
    }


def test_report_groups_departments_and_preserves_legacy_unknown():
    base = {"kind": "provider_request", "operation": "chat", "outcome": "success"}
    result = ev.summarize(
        [
            {**base, "department": "male", "duration_ms": 100},
            {**base, "department": "female", "duration_ms": 400},
            {**base, "duration_ms": 200},
        ]
    )
    groups = {r["department"]: r for r in result["operations"]}
    assert set(groups) == {"male", "female", None}
    assert groups["male"]["latency_ms"]["mean"] == 100
    assert groups["female"]["latency_ms"]["mean"] == 400
    assert groups[None]["latency_ms"]["mean"] == 200


def test_report_command_can_filter_department(monkeypatch, tmp_path):
    source = tmp_path / "events.jsonl"
    output = tmp_path / "report.json"
    base = {"operation": "chat", "kind": "provider_request", "outcome": "success"}
    source.write_text(
        "\n".join(
            json.dumps(row)
            for row in [
                base,
                {**base, "department": "male"},
                {**base, "department": "female"},
            ]
        )
        + "\n"
    )
    monkeypatch.setattr(
        "sys.argv",
        [
            "evaluation",
            "--input",
            str(source),
            "--output",
            str(output),
            "--department",
            "female",
        ],
    )
    ev.main()
    report = json.loads(output.read_text())
    assert report["records"] == 1 and report["operations"][0]["department"] == "female"
    monkeypatch.setattr(
        "sys.argv",
        [
            "evaluation",
            "--input",
            str(source),
            "--output",
            str(output),
            "--department",
            "unknown",
        ],
    )
    ev.main()
    report = json.loads(output.read_text())
    assert report["records"] == 1 and report["operations"][0]["department"] is None


def test_csv_recording_roundtrip_preserves_usage_nulls_and_zero(monkeypatch, tmp_path):
    import csv

    path = tmp_path / "events.csv"
    monkeypatch.setenv("EVALUATION_FILE", str(path))
    with ev.evaluation_context(department="male"):
        ev.record_event(
            "chat",
            "success",
            kind="provider_request",
            duration_ms=12.5,
            cache_hit=False,
            tokens={"input": 10, "output": 0, "total": 10},
            usage={"prompt_tokens": 10, "completion_tokens": 0},
        )
    ev.record_event("search", "success", kind="api_request", duration_ms=0)
    restored, invalid = ev.read_events(path)
    assert invalid == 0 and len(restored) == 2
    assert restored[0]["tokens"] == {"input": 10, "output": 0, "total": 10}
    assert (
        restored[0]["cache_hit"] is False
        and restored[0]["usage"]["completion_tokens"] == 0
    )
    assert restored[1]["department"] is None and restored[1]["tokens"] is None
    assert restored[1]["duration_ms"] == 0
    with path.open(newline="") as stream:
        assert next(csv.reader(stream)).count("operation") == 1


def test_csv_summary_and_event_export_from_jsonl(monkeypatch, tmp_path):
    import csv

    source = tmp_path / "events.jsonl"
    summary = tmp_path / "summary.csv"
    export = tmp_path / "export.csv"
    source.write_text(
        json.dumps(
            {
                "event_id": "one",
                "operation": "chat",
                "kind": "provider_request",
                "outcome": "success",
                "department": "male",
                "duration_ms": 50,
                "tokens": {"input": 10, "output": 2, "total": 12},
            }
        )
        + "\n"
    )
    monkeypatch.setattr(
        "sys.argv",
        [
            "evaluation",
            "--input",
            str(source),
            "--output",
            str(summary),
            "--export-events",
            str(export),
        ],
    )
    ev.main()
    with summary.open(newline="") as stream:
        rows = list(csv.DictReader(stream))
    assert len(rows) == 1 and rows[0]["department"] == "male"
    assert (
        float(rows[0]["latency_ms.p95"]) == 50 and rows[0]["tokens.total.sum"] == "12"
    )
    restored, invalid = ev.read_events(export)
    assert invalid == 0 and restored[0]["event_id"] == "one"


def test_default_report_combines_csv_and_legacy_without_double_count(
    monkeypatch, tmp_path
):
    directory = tmp_path / "data"
    directory.mkdir()
    current = directory / "evaluation.csv"
    legacy = directory / "evaluation.jsonl"
    summary = tmp_path / "summary.json"
    row = {
        "event_id": "one",
        "operation": "chat",
        "kind": "provider_request",
        "outcome": "success",
    }
    ev.write_event_csv(current, [row, {**row, "event_id": "two"}])
    legacy.write_text(json.dumps(row) + "\n")
    monkeypatch.delenv("EVALUATION_FILE")
    monkeypatch.setattr(ev, "__file__", str(tmp_path / "app" / "evaluation.py"))
    monkeypatch.setattr("sys.argv", ["evaluation", "--output", str(summary)])
    ev.main()
    assert json.loads(summary.read_text())["records"] == 2
