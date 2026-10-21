"""Flask application: serves the UI and the JSON API."""

import logging

from flask import Flask, jsonify, render_template, request

import assistant
import detector
from prompts import PROMPT_VARIANTS

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("verifact")


def create_app() -> Flask:
    app = Flask(__name__)
    # Request body limit (bytes): context + answer limits plus JSON overhead
    app.config["MAX_CONTENT_LENGTH"] = 64 * 1024

    @app.get("/")
    def index():
        return render_template(
            "index.html",
            max_context=detector.MAX_CONTEXT_CHARS,
            max_answer=detector.MAX_ANSWER_CHARS,
        )

    @app.get("/api/health")
    def health():
        return jsonify({"status": "ok", "configured": detector.is_configured()})

    @app.post("/api/verify")
    def verify():
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict):
            return jsonify({"error": "Request body must be JSON with 'context' and 'answer'."}), 400

        variant = payload.get("prompt_variant", "improved")
        if variant not in PROMPT_VARIANTS:
            return jsonify({"error": f"Unknown prompt variant. Choose from: {', '.join(PROMPT_VARIANTS)}."}), 400

        try:
            result = detector.verify_answer(
                payload.get("context"), payload.get("answer"), variant
            )
            return jsonify(result), 200
        except detector.InputValidationError as exc:
            return jsonify({"error": str(exc)}), 400
        except detector.VerificationError as exc:
            logger.warning("Verification failed: %s", exc)
            return jsonify({"error": str(exc), "type": "verification_failed"}), 502
        except Exception:  # unexpected: log details server-side, hide them from users
            logger.exception("Unexpected error in /api/verify")
            return jsonify({"error": "An unexpected server error occurred."}), 500

    @app.post("/api/assistant")
    def assistant_route():
        """Optional assistant features. Failures here never affect /api/verify."""
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict):
            return jsonify({"error": "Request body must be JSON."}), 400
        try:
            return jsonify(assistant.run_action(payload)), 200
        except detector.InputValidationError as exc:
            return jsonify({"error": str(exc)}), 400
        except detector.VerificationError as exc:
            logger.warning("Assistant failed: %s", exc)
            return jsonify({"error": str(exc), "type": "assistant_failed"}), 502
        except Exception:
            logger.exception("Unexpected error in /api/assistant")
            return jsonify({"error": "An unexpected server error occurred."}), 500

    @app.errorhandler(413)
    def too_large(_):
        return jsonify({"error": "Request is too large."}), 413

    @app.errorhandler(404)
    def not_found(_):
        return jsonify({"error": "Not found."}), 404

    return app


app = create_app()

if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=False)
