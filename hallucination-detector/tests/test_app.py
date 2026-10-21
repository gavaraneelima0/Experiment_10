"""Flask route tests. The detector is mocked, so no API key or quota is used."""
import detector
from app import create_app


def make_client():
    app = create_app()
    app.config["TESTING"] = True
    return app.test_client()


GOOD_RESULT = {
    "results": [{"claim": "c", "label": "SUPPORTED", "evidence": "e", "explanation": "x",
                 "evidence_found_in_context": True}],
    "summary": {"total": 1, "supported": 1, "contradicted": 0, "not_verifiable": 0},
    "assessment": {"contradiction_rate": 0.0, "message": "ok"},
    "meta": {"prompt_variant": "improved", "model": "test"},
}


def test_home_route():
    res = make_client().get("/")
    assert res.status_code == 200
    assert b"VeriFact AI" in res.data


def test_health_endpoint_hides_secrets(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "super-secret-value")
    res = make_client().get("/api/health")
    assert res.status_code == 200
    body = res.get_json()
    assert body == {"status": "ok", "configured": True}
    assert b"super-secret-value" not in res.data


def test_health_reports_unconfigured(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    assert make_client().get("/api/health").get_json()["configured"] is False


def test_verify_success_with_mocked_detector(monkeypatch):
    monkeypatch.setattr(detector, "verify_answer", lambda c, a, v="improved": GOOD_RESULT)
    res = make_client().post("/api/verify", json={"context": "ctx", "answer": "ans"})
    assert res.status_code == 200
    assert res.get_json()["summary"]["supported"] == 1


def test_verify_rejects_non_json():
    res = make_client().post("/api/verify", data="hello", content_type="text/plain")
    assert res.status_code == 400


def test_verify_rejects_empty_fields():
    c = make_client()
    assert c.post("/api/verify", json={"context": "", "answer": "x"}).status_code == 400
    assert c.post("/api/verify", json={"context": "x", "answer": "  "}).status_code == 400
    assert c.post("/api/verify", json={}).status_code == 400


def test_verify_rejects_bad_variant():
    res = make_client().post("/api/verify", json={"context": "a", "answer": "b", "prompt_variant": "zzz"})
    assert res.status_code == 400


def test_upstream_failure_returns_502(monkeypatch):
    def boom(c, a, v="improved"):
        raise detector.UpstreamError("Gemini quota or rate limit reached.")
    monkeypatch.setattr(detector, "verify_answer", boom)
    res = make_client().post("/api/verify", json={"context": "c", "answer": "a"})
    assert res.status_code == 502
    assert "quota" in res.get_json()["error"]


def test_missing_key_returns_502(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    res = make_client().post("/api/verify", json={"context": "c", "answer": "a"})
    assert res.status_code == 502
    assert "GEMINI_API_KEY" in res.get_json()["error"]


def test_unexpected_error_returns_500_without_details(monkeypatch):
    def boom(c, a, v="improved"):
        raise RuntimeError("secret internal detail")
    monkeypatch.setattr(detector, "verify_answer", boom)
    res = make_client().post("/api/verify", json={"context": "c", "answer": "a"})
    assert res.status_code == 500
    assert b"secret internal detail" not in res.data


def test_oversized_request_rejected():
    res = make_client().post("/api/verify", json={"context": "x" * 200000, "answer": "y"})
    assert res.status_code == 413
