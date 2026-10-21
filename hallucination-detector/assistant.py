"""Optional assistant features built on top of the verification engine.

    explain_claim(...)     - explain one claim's verification result
    suggest_safer_answer() - rewrite the answer using only context-supported information
    improve_prompt(...)    - suggest a better prompt for an AI model

These reuse detector.call_model / parse_model_json / validate_results. They are stateless:
the browser sends back the earlier results, which are re-validated here (never trusted).
Failures raise the same exceptions as detector.py and never affect core verification.
"""

import detector
import prompts
from detector import InputValidationError, InvalidModelResponse

ACTIONS = ("explain", "safer", "prompt")
MAX_QUESTION_CHARS = 500
MAX_PROMPT_CHARS = 2000
MAX_RESULTS = 40

DISCLAIMER = (
    "AI-generated suggestion based only on your reference context. "
    "It is not guaranteed to be correct; please review it yourself."
)


def _prepare(context, answer, results):
    context, answer = detector.validate_inputs(context, answer)
    if not isinstance(results, list) or not results:
        raise InputValidationError("Run a verification first so there are results to work with.")
    if len(results) > MAX_RESULTS:
        raise InputValidationError("Too many results were submitted.")
    try:
        clean = detector.validate_results({"results": results}, context)
    except InvalidModelResponse:
        raise InputValidationError("The submitted verification results were not valid.")
    return context, answer, clean


def _text(data, key, required=True):
    value = data.get(key, "")
    if not isinstance(value, str):
        raise InvalidModelResponse(f"The assistant response had an invalid '{key}' field.")
    value = value.strip()
    if required and not value:
        raise InvalidModelResponse(f"The assistant response was missing '{key}'.")
    return value


def _text_list(data, key):
    value = data.get(key, [])
    if not isinstance(value, list):
        raise InvalidModelResponse(f"The assistant response had an invalid '{key}' list.")
    return [v.strip() for v in value if isinstance(v, str) and v.strip()]


def _run(prompt):
    return detector.parse_model_json(detector.call_model(prompt))


def explain_claim(context, answer, results, claim_index, question=""):
    context, answer, results = _prepare(context, answer, results)
    if isinstance(claim_index, bool) or not isinstance(claim_index, int) or not 0 <= claim_index < len(results):
        raise InputValidationError("Select a claim from the results first.")
    if not isinstance(question, str):
        raise InputValidationError("The question must be text.")
    question = question.strip()
    if len(question) > MAX_QUESTION_CHARS:
        raise InputValidationError(f"The question is too long (max {MAX_QUESTION_CHARS} characters).")

    data = _run(prompts.build_explain_prompt(context, answer, results, claim_index, question))
    evidence = _text(data, "evidence", required=False)
    return {
        "action": "explain",
        "claim": results[claim_index]["claim"],
        "label": results[claim_index]["label"],
        "explanation": _text(data, "explanation"),
        "evidence": evidence,
        "evidence_found_in_context": (not evidence) or detector._squash(evidence) in detector._squash(context),
        "assumptions": _text(data, "assumptions", required=False),
        "disclaimer": DISCLAIMER,
    }


def suggest_safer_answer(context, answer, results):
    context, answer, results = _prepare(context, answer, results)
    data = _run(prompts.build_safer_prompt(context, answer, results))
    return {
        "action": "safer",
        "safer_answer": _text(data, "safer_answer"),
        "changes": _text_list(data, "changes"),
        "unverifiable_items": _text_list(data, "unverifiable_items"),
        "disclaimer": DISCLAIMER,
    }


def improve_prompt(context, answer, results, original_prompt=""):
    context, answer, results = _prepare(context, answer, results)
    if not isinstance(original_prompt, str):
        raise InputValidationError("The original prompt must be text.")
    original_prompt = original_prompt.strip()
    if len(original_prompt) > MAX_PROMPT_CHARS:
        raise InputValidationError(f"The original prompt is too long (max {MAX_PROMPT_CHARS} characters).")
    data = _run(prompts.build_improve_prompt(context, answer, results, original_prompt))
    return {
        "action": "prompt",
        "improved_prompt": _text(data, "improved_prompt"),
        "reasons": _text_list(data, "reasons"),
        "assumptions": _text(data, "assumptions", required=False),
        "disclaimer": DISCLAIMER,
    }


def run_action(payload: dict) -> dict:
    """Dispatch an assistant request (payload from the API)."""
    action = payload.get("action")
    ctx, ans, res = payload.get("context"), payload.get("answer"), payload.get("results")
    if action == "explain":
        return explain_claim(ctx, ans, res, payload.get("claim_index"), payload.get("question", ""))
    if action == "safer":
        return suggest_safer_answer(ctx, ans, res)
    if action == "prompt":
        return improve_prompt(ctx, ans, res, payload.get("original_prompt", ""))
    raise InputValidationError(f"Unknown action. Choose from: {', '.join(ACTIONS)}.")
