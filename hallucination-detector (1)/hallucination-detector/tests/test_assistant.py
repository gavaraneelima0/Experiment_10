"""Tests for the optional assistant. The Gemini call (detector.call_model) is always mocked."""
import json

import pytest

import assistant
import detector
import prompts
from app import create_app

CONTEXT = "The tower was completed in 1889. It is 330 metres tall."
ANSWER = "It was completed in 1900. It is 330 metres tall. It gets 7 million visitors."
RESULTS = [
    {"claim": "It was completed in 1900.", "label": "CONTRADICTED", "evidence": "completed in 1889", "explanation": "Date differs."},
    {"claim": "It is 330 metres tall.", "label": "SUPPORTED", "evidence": "330 metres tall", "explanation": "Matches."},
    {"claim": "It gets 7 million visitors.", "label": "NOT VERIFIABLE", "evidence": "", "explanation": "Not mentioned."},
]


def mock_model(monkeypatch, payload):
    seen = {}

    def fake(prompt):
        seen["prompt"] = prompt
        return payload if isinstance(payload, str) else json.dumps(payload)

    monkeypatch.setattr(detector, "call_model", fake)
    return seen


def client():
    app = create_app()
    app.config["TESTING"] = True
    return app.test_client()


# ---------------------------------------------------------------- explain
def test_explain_claim(monkeypatch):
    seen = mock_model(monkeypatch, {"explanation": "The context says 1889.", "evidence": "completed in 1889", "assumptions": ""})
    out = assistant.explain_claim(CONTEXT, ANSWER, RESULTS, 0, "Why?")
    assert out["label"] == "CONTRADICTED"
    assert out["evidence_found_in_context"] is True
    assert "TARGET_CLAIM" in seen["prompt"] and "Why?" in seen["prompt"]
    assert "guaranteed" in out["disclaimer"]


def test_explain_flags_quote_not_in_context(monkeypatch):
    mock_model(monkeypatch, {"explanation": "x", "evidence": "invented quote", "assumptions": "a guess"})
    out = assistant.explain_claim(CONTEXT, ANSWER, RESULTS, 1)
    assert out["evidence_found_in_context"] is False
    assert out["assumptions"] == "a guess"


def test_explain_bad_claim_index():
    for bad in (-1, 3, "0", None, True):
        with pytest.raises(detector.InputValidationError):
            assistant.explain_claim(CONTEXT, ANSWER, RESULTS, bad)


def test_explain_question_too_long():
    with pytest.raises(detector.InputValidationError):
        assistant.explain_claim(CONTEXT, ANSWER, RESULTS, 0, "q" * 501)


# ------------------------------------------------------------------ safer
def test_safer_answer(monkeypatch):
    mock_model(monkeypatch, {"safer_answer": "It was completed in 1889 and is 330 metres tall.",
                             "changes": ["Corrected the year."], "unverifiable_items": ["7 million visitors"]})
    out = assistant.suggest_safer_answer(CONTEXT, ANSWER, RESULTS)
    assert "1889" in out["safer_answer"]
    assert out["unverifiable_items"] == ["7 million visitors"]
    assert "not guaranteed" in out["disclaimer"]


def test_safer_answer_missing_field_rejected(monkeypatch):
    mock_model(monkeypatch, {"changes": []})
    with pytest.raises(detector.InvalidModelResponse):
        assistant.suggest_safer_answer(CONTEXT, ANSWER, RESULTS)


# ----------------------------------------------------------------- prompt
def test_improve_prompt(monkeypatch):
    seen = mock_model(monkeypatch, {"improved_prompt": "Answer using only {context}. Say 'not stated' if missing.",
                                    "reasons": ["Limits the model to the context."], "assumptions": ""})
    out = assistant.improve_prompt(CONTEXT, ANSWER, RESULTS, "Tell me about the tower")
    assert "{context}" in out["improved_prompt"]
    assert "ORIGINAL_PROMPT" in seen["prompt"] and "Tell me about the tower" in seen["prompt"]


def test_improve_prompt_works_without_original(monkeypatch):
    mock_model(monkeypatch, {"improved_prompt": "Use only the context.", "reasons": [], "assumptions": ""})
    assert assistant.improve_prompt(CONTEXT, ANSWER, RESULTS)["improved_prompt"]


# ------------------------------------------------------ validation & safety
def test_requires_results():
    for bad in (None, [], "x"):
        with pytest.raises(detector.InputValidationError):
            assistant.suggest_safer_answer(CONTEXT, ANSWER, bad)


def test_client_supplied_results_are_revalidated():
    bad = [{"claim": "c", "label": "TRUE", "evidence": "", "explanation": ""}]
    with pytest.raises(detector.InputValidationError):
        assistant.suggest_safer_answer(CONTEXT, ANSWER, bad)


def test_empty_context_rejected():
    with pytest.raises(detector.InputValidationError):
        assistant.suggest_safer_answer("", ANSWER, RESULTS)


def test_malformed_model_json(monkeypatch):
    mock_model(monkeypatch, "not json at all")
    with pytest.raises(detector.InvalidModelResponse):
        assistant.suggest_safer_answer(CONTEXT, ANSWER, RESULTS)


def test_assistant_prompts_have_injection_defence_and_evidence_rules():
    for p in (
        prompts.build_explain_prompt(CONTEXT, ANSWER, RESULTS, 0, "q"),
        prompts.build_safer_prompt(CONTEXT, ANSWER, RESULTS),
        prompts.build_improve_prompt(CONTEXT, ANSWER, RESULTS, "orig"),
    ):
        assert "untrusted" in p and "Never follow it" in p
        assert "EVIDENCE VS ASSUMPTIONS" in p and "guaranteed" in p


# -------------------------------------------------------------- API route
def payload(action, **extra):
    base = {"action": action, "context": CONTEXT, "answer": ANSWER, "results": RESULTS}
    base.update(extra)
    return base


def test_route_explain(monkeypatch):
    mock_model(monkeypatch, {"explanation": "ok", "evidence": "", "assumptions": ""})
    res = client().post("/api/assistant", json=payload("explain", claim_index=2))
    assert res.status_code == 200 and res.get_json()["action"] == "explain"


def test_route_safer_and_prompt(monkeypatch):
    mock_model(monkeypatch, {"safer_answer": "a", "changes": [], "unverifiable_items": [],
                             "improved_prompt": "b", "reasons": [], "assumptions": ""})
    assert client().post("/api/assistant", json=payload("safer")).status_code == 200
    assert client().post("/api/assistant", json=payload("prompt")).status_code == 200


def test_route_invalid_requests():
    c = client()
    assert c.post("/api/assistant", data="x", content_type="text/plain").status_code == 400
    assert c.post("/api/assistant", json=payload("dance")).status_code == 400
    assert c.post("/api/assistant", json=payload("explain")).status_code == 400  # no claim_index
    assert c.post("/api/assistant", json={"action": "safer", "context": "a", "answer": "b"}).status_code == 400


def test_route_upstream_failure_returns_502(monkeypatch):
    def boom(prompt):
        raise detector.UpstreamError("Gemini quota or rate limit reached.")
    monkeypatch.setattr(detector, "call_model", boom)
    res = client().post("/api/assistant", json=payload("safer"))
    assert res.status_code == 502


def test_assistant_failure_does_not_break_core_verification(monkeypatch):
    """Core /api/verify keeps working when the assistant fails."""
    calls = {"n": 0}

    def flaky(prompt):
        calls["n"] += 1
        if "VERIFICATION_RESULTS" in prompt:
            raise detector.UpstreamError("assistant down")
        return json.dumps({"results": [{"claim": "c", "label": "SUPPORTED", "evidence": "", "explanation": "x"}]})

    monkeypatch.setattr(detector, "call_model", flaky)
    c = client()
    assert c.post("/api/assistant", json=payload("safer")).status_code == 502
    ok = c.post("/api/verify", json={"context": CONTEXT, "answer": ANSWER})
    assert ok.status_code == 200 and ok.get_json()["summary"]["supported"] == 1
