"""Claim verification logic: Gemini call, response parsing, validation, summary.

Public interface:
    verify_answer(context, answer, prompt_variant="improved") -> dict

Exceptions (all subclass VerificationError except InputValidationError):
    InputValidationError   - empty / too-long / wrongly-typed input (maps to HTTP 400)
    ConfigurationError     - API key missing (maps to HTTP 502 in the API)
    UpstreamError          - Gemini unreachable, quota exhausted, model unavailable, timeout
    InvalidModelResponse   - model replied with malformed JSON or invalid labels

No fallback results are ever fabricated: any failure raises an exception.
"""

import json
import os
import re

from dotenv import load_dotenv

from prompts import PROMPT_VARIANTS, build_prompt

load_dotenv()

VALID_LABELS = ("SUPPORTED", "CONTRADICTED", "NOT VERIFIABLE")
MAX_CONTEXT_CHARS = 8000
MAX_ANSWER_CHARS = 4000
DEFAULT_MODEL = "gemini-flash-latest"
DEFAULT_TIMEOUT_SECONDS = 60


class VerificationError(Exception):
    """Base class for verification failures that are not the user's fault."""


class InputValidationError(ValueError):
    """The user supplied invalid input."""


class ConfigurationError(VerificationError):
    """The detector is not configured (e.g. missing API key)."""


class UpstreamError(VerificationError):
    """The Gemini API call failed (network, quota, model, timeout)."""


class InvalidModelResponse(VerificationError):
    """The model returned something that could not be validated."""


# ---------------------------------------------------------------- configuration
def get_api_key() -> str:
    return os.getenv("GEMINI_API_KEY", "").strip()


def get_model_name() -> str:
    return os.getenv("GEMINI_MODEL", DEFAULT_MODEL).strip() or DEFAULT_MODEL


def is_configured() -> bool:
    """True if an API key looks present. Never exposes the key itself."""
    key = get_api_key()
    return bool(key) and key != "your_api_key_here"


# ------------------------------------------------------------------ validation
def validate_inputs(context, answer) -> tuple[str, str]:
    if not isinstance(context, str) or not isinstance(answer, str):
        raise InputValidationError("Context and answer must be text.")
    context, answer = context.strip(), answer.strip()
    if not context:
        raise InputValidationError("Reference context cannot be empty.")
    if not answer:
        raise InputValidationError("AI-generated answer cannot be empty.")
    if len(context) > MAX_CONTEXT_CHARS:
        raise InputValidationError(f"Reference context is too long (max {MAX_CONTEXT_CHARS} characters).")
    if len(answer) > MAX_ANSWER_CHARS:
        raise InputValidationError(f"Answer is too long (max {MAX_ANSWER_CHARS} characters).")
    return context, answer


def parse_model_json(text: str) -> dict:
    """Parse model output into a dict, repairing common harmless problems
    (markdown fences, text around the JSON object). Raises InvalidModelResponse."""
    if not isinstance(text, str) or not text.strip():
        raise InvalidModelResponse("The model returned an empty response.")
    cleaned = text.strip()
    cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\s*```$", "", cleaned)
    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError:
        start, end = cleaned.find("{"), cleaned.rfind("}")
        if start == -1 or end <= start:
            raise InvalidModelResponse("The model response was not valid JSON.")
        try:
            data = json.loads(cleaned[start : end + 1])
        except json.JSONDecodeError:
            raise InvalidModelResponse("The model response was not valid JSON.")
    if isinstance(data, list):  # tolerate a bare list of results
        data = {"results": data}
    if not isinstance(data, dict):
        raise InvalidModelResponse("The model response had an unexpected structure.")
    return data


def _normalize_label(raw) -> str:
    if not isinstance(raw, str):
        raise InvalidModelResponse("A claim had a missing or non-text label.")
    label = re.sub(r"[\s_\-]+", " ", raw.strip().upper())
    if label not in VALID_LABELS:
        raise InvalidModelResponse(f"Invalid label from model: {raw!r}.")
    return label


def _squash(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().lower()


def validate_results(data: dict, context: str = "") -> list[dict]:
    """Validate the model's 'results' list and return clean result dicts.

    Adds 'evidence_found_in_context' so the UI can flag quotes that do not
    appear verbatim in the reference context (possible fabrication)."""
    results = data.get("results")
    if not isinstance(results, list):
        raise InvalidModelResponse("The model response is missing a 'results' list.")
    ctx = _squash(context)
    clean = []
    for item in results:
        if not isinstance(item, dict):
            raise InvalidModelResponse("A result entry was not an object.")
        claim = item.get("claim")
        if not isinstance(claim, str) or not claim.strip():
            raise InvalidModelResponse("A result entry had an empty claim.")
        label = _normalize_label(item.get("label"))
        evidence = item.get("evidence", "")
        explanation = item.get("explanation", "")
        evidence = evidence.strip() if isinstance(evidence, str) else ""
        explanation = explanation.strip() if isinstance(explanation, str) else ""
        evidence_ok = (not evidence) or (_squash(evidence) in ctx)
        clean.append(
            {
                "claim": claim.strip(),
                "label": label,
                "evidence": evidence,
                "explanation": explanation,
                "evidence_found_in_context": evidence_ok,
            }
        )
    return clean


def compute_summary(results: list[dict]) -> dict:
    """Count labels from validated results (never trusted from the model)."""
    supported = sum(1 for r in results if r["label"] == "SUPPORTED")
    contradicted = sum(1 for r in results if r["label"] == "CONTRADICTED")
    not_verifiable = sum(1 for r in results if r["label"] == "NOT VERIFIABLE")
    return {
        "total": len(results),
        "supported": supported,
        "contradicted": contradicted,
        "not_verifiable": not_verifiable,
    }


def compute_assessment(summary: dict) -> dict:
    """Transparent heuristic: contradiction rate = contradicted / total claims.

    NOT VERIFIABLE claims are NOT counted as contradicted. The rate is a concern
    indicator relative to the supplied context, not a probability of hallucination."""
    total = summary["total"]
    if total == 0:
        return {
            "contradiction_rate": None,
            "message": "No verifiable factual claims were found in the answer.",
        }
    rate = round(100 * summary["contradicted"] / total, 1)
    return {
        "contradiction_rate": rate,
        "message": (
            f"{summary['contradicted']} of {total} claims directly conflict with the supplied context "
            f"({rate}%). This is a concern indicator relative to the context only, "
            "not the probability that the whole answer is hallucinated."
        ),
    }


# ------------------------------------------------------------------ Gemini call
def call_model(prompt: str) -> str:
    """Send the prompt to Gemini and return the raw text. Raises ConfigurationError/UpstreamError."""
    if not is_configured():
        raise ConfigurationError("GEMINI_API_KEY is not set. Add it to your .env file.")
    try:
        from google import genai
        from google.genai import types
    except ImportError as exc:
        raise ConfigurationError(
            "The 'google-genai' package is not installed. Run: python -m pip install -r requirements.txt"
        ) from exc

    try:
        timeout_s = float(os.getenv("GEMINI_TIMEOUT_SECONDS", DEFAULT_TIMEOUT_SECONDS))
    except ValueError:
        timeout_s = DEFAULT_TIMEOUT_SECONDS

    try:
        client = genai.Client(
            api_key=get_api_key(),
            http_options=types.HttpOptions(timeout=int(timeout_s * 1000)),  # milliseconds
        )
        response = client.models.generate_content(
            model=get_model_name(),
            contents=prompt,
            config=types.GenerateContentConfig(
                temperature=0,
                response_mime_type="application/json",
            ),
        )
        return response.text or ""
    except Exception as exc:  # map SDK/network errors to safe messages
        raise _translate_api_error(exc) from exc


def _translate_api_error(exc: Exception) -> UpstreamError:
    code = getattr(exc, "code", None)
    name = type(exc).__name__.lower()
    text = str(exc).lower()
    if code == 429 or "resource_exhausted" in text or "quota" in text:
        return UpstreamError("Gemini quota or rate limit reached. Wait a minute and retry, or check your API plan.")
    if code == 404 or "not found" in text:
        return UpstreamError(
            f"The model '{get_model_name()}' is unavailable for your key. Run python list_models.py to see usable models, then set GEMINI_MODEL in .env."
        )
    if code in (401, 403) or "api key" in text or "permission" in text:
        return UpstreamError("Gemini rejected the API key. Check GEMINI_API_KEY in .env.")
    if code in (500, 503) or "unavailable" in text or "overloaded" in text:
        return UpstreamError("Gemini is temporarily unavailable. Please retry shortly.")
    if "timeout" in name or "timed out" in text or "timeout" in text:
        return UpstreamError("The request to Gemini timed out. Please retry.")
    if "connect" in name or "connection" in text or "network" in text:
        return UpstreamError("Could not reach Gemini. Check your internet connection.")
    return UpstreamError("The Gemini request failed. Please retry.")


# ----------------------------------------------------------------- main entry
def verify_answer(context: str, answer: str, prompt_variant: str = "improved") -> dict:
    """Verify claims in `answer` against `context`.

    Returns {"results": [...], "summary": {...}, "assessment": {...}, "meta": {...}}.
    Raises InputValidationError, ConfigurationError, UpstreamError or InvalidModelResponse."""
    context, answer = validate_inputs(context, answer)
    if prompt_variant not in PROMPT_VARIANTS:
        raise InputValidationError(f"Unknown prompt variant. Choose from: {', '.join(PROMPT_VARIANTS)}.")

    prompt = build_prompt(context, answer, prompt_variant)
    raw = call_model(prompt)
    data = parse_model_json(raw)
    results = validate_results(data, context)
    summary = compute_summary(results)
    assert summary["total"] == summary["supported"] + summary["contradicted"] + summary["not_verifiable"]
    return {
        "results": results,
        "summary": summary,
        "assessment": compute_assessment(summary),
        "meta": {"prompt_variant": prompt_variant, "model": get_model_name()},
    }
