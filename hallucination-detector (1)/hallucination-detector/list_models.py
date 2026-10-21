"""Lists the Gemini models your API key can use for text generation.

Run:  python list_models.py
Then copy one model name (without the "models/" prefix) into GEMINI_MODEL in .env.
"""
import detector

if not detector.is_configured():
    raise SystemExit("GEMINI_API_KEY is not set. Add it to your .env file first.")

from google import genai

client = genai.Client(api_key=detector.get_api_key())
names = []
for m in client.models.list():
    actions = getattr(m, "supported_actions", None) or []
    if "generateContent" in actions:
        names.append(m.name.replace("models/", ""))

print("Models available for your key:")
for n in sorted(names):
    print("  ", n)
