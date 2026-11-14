"""Voice Form Assistant — Flask entry point (App Service expects app.py).

Checkpoint scope (Parts A–C):
  POST /transcribe   audio -> transcript              (Part B)
  POST /analyze      {"text"} -> entities + fields     (Part C)
  POST /fill-form    audio -> transcript -> fields     (main endpoint, C2)
Later: TTS read-back (D), frontend (E), telemetry (G), /confirm (Track 1).
"""
from dotenv import load_dotenv
load_dotenv()  # must run before anything reads os.environ

# Part G: telemetry init goes HERE, before Flask / Azure SDK imports.
# from telemetry import init_telemetry
# init_telemetry()

import os
import uuid

from flask import Flask, jsonify, request
from werkzeug.exceptions import RequestEntityTooLarge

from form_extractor import extract_fields
from language_service import LanguageServiceError, analyze
from speech_service import SpeechServiceError, UnsupportedAudioError, transcribe

ALLOWED_EXTENSIONS = {"wav", "mp3", "webm", "ogg", "m4a"}
TEMP_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "temp_audio")
os.makedirs(TEMP_DIR, exist_ok=True)

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 25 * 1024 * 1024  # 25 MB


def _error(status, message):
    return jsonify({"error": message}), status


@app.errorhandler(RequestEntityTooLarge)
def too_large(_):
    return _error(413, "File too large (max 25 MB).")


@app.errorhandler(UnsupportedAudioError)
def unsupported_audio(e):
    return _error(415, str(e))


@app.errorhandler(SpeechServiceError)
@app.errorhandler(LanguageServiceError)
def upstream_error(e):
    return _error(502, str(e))


def _save_upload():
    """Validate the multipart 'audio' field and save it to temp_audio/."""
    f = request.files.get("audio")
    if f is None or f.filename == "":
        return None, _error(400, "Send the audio as multipart field 'audio'.")
    ext = f.filename.rsplit(".", 1)[-1].lower() if "." in f.filename else ""
    if ext not in ALLOWED_EXTENSIONS:
        return None, _error(415, f"Unsupported file type '.{ext}'. "
                                 f"Use one of: {', '.join(sorted(ALLOWED_EXTENSIONS))}.")
    path = os.path.join(TEMP_DIR, f"{uuid.uuid4().hex}.{ext}")
    f.save(path)
    return path, None


@app.post("/transcribe")
def transcribe_endpoint():
    path, err = _save_upload()
    if err:
        return err
    try:
        return jsonify(transcribe(path))
    finally:
        os.remove(path)  # never keep user audio


@app.post("/analyze")
def analyze_endpoint():
    body = request.get_json(silent=True) or {}
    text = (body.get("text") or "").strip()
    if not text:
        return _error(400, "JSON body must contain a non-empty 'text'.")
    lang = analyze(text)
    return jsonify({**lang, **extract_fields(text, lang["entities"])})


@app.post("/fill-form")
def fill_form_endpoint():
    path, err = _save_upload()
    if err:
        return err
    try:
        stt = transcribe(path)
    finally:
        os.remove(path)
    if not stt["transcript"]:
        return _error(422, "No speech was recognized in the audio.")
    lang = analyze(stt["transcript"])
    form = extract_fields(stt["transcript"], lang["entities"])
    return jsonify({
        "transcript": stt["transcript"],
        "confidence": stt["confidence"],
        "duration_seconds": stt["duration_seconds"],
        **form,
        "language_analysis": lang,
    })


@app.get("/health")
def health():
    return jsonify({"status": "ok"})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))  # macOS AirPlay occupies 5000
    app.run(host="0.0.0.0", port=port, debug=True)
