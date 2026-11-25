"""Voice Form Assistant — Flask entry point (App Service expects app.py).

  POST /transcribe          audio -> transcript                      (Part B)
  POST /analyze             {"text"} -> entities + fields             (Part C)
  POST /fill-form, /process audio -> STT -> Language -> TTS read-back (C2 + D)
  GET  /voices              the three TTS voices                     (D3)
  GET  /telemetry-summary   in-memory pipeline stats                 (G8)
  GET  /                    minimal upload / record page            (E, basic)
  GET  /health
Later: full frontend (E), /confirm (Track 1).
"""
from dotenv import load_dotenv
load_dotenv()  # must run before anything reads os.environ

# Part G: telemetry must start BEFORE Flask / Azure SDK imports (Pitfall 1).
from telemetry import init_telemetry
init_telemetry()

import os
import time
import uuid

from flask import Flask, jsonify, request, send_from_directory
from opentelemetry import trace
from werkzeug.exceptions import RequestEntityTooLarge

import telemetry
from form_extractor import extract_fields
from language_service import LanguageServiceError, analyze
from speech_service import SpeechServiceError, UnsupportedAudioError, transcribe
from tts_service import VOICES, build_summary, resolve_voice, synthesize

ALLOWED_EXTENSIONS = {"wav", "mp3", "webm", "ogg", "m4a"}
TEMP_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "temp_audio")
os.makedirs(TEMP_DIR, exist_ok=True)

tracer = trace.get_tracer(telemetry.SERVICE_NAME)

# Bump on every release; the CI/CD workflow's deploy is verified by reading it back.
APP_VERSION = "1.1.0"

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 25 * 1024 * 1024  # 25 MB


class NoSpeechError(Exception):
    """The audio decoded fine but contained no recognizable speech -> 422."""


def _error(status, message):
    return jsonify({"error": message}), status


@app.errorhandler(RequestEntityTooLarge)
def too_large(_):
    return _error(413, "File too large (max 25 MB).")


@app.errorhandler(UnsupportedAudioError)
def unsupported_audio(e):
    return _error(415, str(e))


@app.errorhandler(NoSpeechError)
def no_speech(e):
    return _error(422, str(e))


@app.errorhandler(SpeechServiceError)
@app.errorhandler(LanguageServiceError)
def upstream_error(e):
    return _error(502, str(e))


def _save_upload():
    """Validate the multipart 'audio' field and save it to temp_audio/."""
    f = request.files.get("audio")
    if f is None or f.filename == "":
        return None, None, _error(400, "Send the audio as multipart field 'audio'.")
    ext = f.filename.rsplit(".", 1)[-1].lower() if "." in f.filename else ""
    if ext not in ALLOWED_EXTENSIONS:
        return None, ext, _error(415, f"Unsupported file type '.{ext}'. "
                                      f"Use one of: {', '.join(sorted(ALLOWED_EXTENSIONS))}.")
    path = os.path.join(TEMP_DIR, f"{uuid.uuid4().hex}.{ext}")
    f.save(path)
    return path, ext, None


@app.post("/transcribe")
def transcribe_endpoint():
    path, _, err = _save_upload()
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


def _ms_since(t0):
    return (time.perf_counter() - t0) * 1000


@app.post("/fill-form")
@app.post("/process")  # the name the assignment uses for the main pipeline
def fill_form_endpoint():
    """Main pipeline: STT -> Language + field extraction -> TTS read-back.

    Optional multipart field 'voice': formal | casual | lively (D3).
    """
    try:
        voice = resolve_voice(request.form.get("voice"))
    except ValueError as e:
        return _error(400, str(e))
    path, audio_format, err = _save_upload()
    if err:
        return err

    request_span = trace.get_current_span()  # Flask auto-instrumented span
    stage = "speech_to_text"
    try:
        with tracer.start_as_current_span("pipeline.process") as root:
            root.set_attribute("audio.format", audio_format)
            root.set_attribute("tts.voice", voice)

            # Stage 1: Speech-to-Text
            with tracer.start_as_current_span("stage.speech_to_text") as span:
                t0 = time.perf_counter()
                try:
                    stt = transcribe(path)
                finally:
                    os.remove(path)
                stt_ms = _ms_since(t0)
                span.set_attribute("stt.confidence", stt["confidence"])
                span.set_attribute("stt.word_count", len(stt["transcript"].split()))
                span.set_attribute("stt.audio_seconds", stt["duration_seconds"])
                span.set_attribute("duration_ms", stt_ms)
            if not stt["transcript"]:
                raise NoSpeechError("No speech was recognized in the audio.")

            # Stage 2: Language analysis + form field extraction
            stage = "language_analysis"
            with tracer.start_as_current_span("stage.language_analysis") as span:
                t0 = time.perf_counter()
                lang = analyze(stt["transcript"])
                form = extract_fields(stt["transcript"], lang["entities"],
                                      words=stt["words"])
                lang_ms = _ms_since(t0)
                span.set_attribute("entity_count", len(lang["entities"]))
                span.set_attribute("sentiment", lang["sentiment"]["label"])
                span.set_attribute("form.fields_correct_first_try",
                                   form["fields_correct_first_try"])
                span.set_attribute("duration_ms", lang_ms)

            # Stage 3: Text-to-Speech read-back
            stage = "text_to_speech"
            with tracer.start_as_current_span("stage.text_to_speech") as span:
                t0 = time.perf_counter()
                summary = build_summary(form, lang)
                tts = synthesize(summary, voice)
                tts_ms = _ms_since(t0)
                span.set_attribute("char_count", tts["char_count"])
                span.set_attribute("tts.voice", tts["voice_name"])
                span.set_attribute("duration_ms", tts_ms)

            timings = {"stt_ms": stt_ms, "language_ms": lang_ms, "tts_ms": tts_ms}
            telemetry.emit_pipeline_metrics(stt, lang, tts, form, timings, audio_format)
            telemetry.mark_pipeline_event(request_span, audio_format,
                                          stt=stt, lang=lang, form=form)
            telemetry.log_pipeline_call(stt, lang, form, timings, voice)
    except Exception as e:
        telemetry.mark_pipeline_event(request_span, audio_format,
                                      error_stage=stage, error=e)
        telemetry.log_pipeline_error(stage)
        raise  # the error handlers above turn it into 415/422/502

    return jsonify({
        "transcript": stt["transcript"],
        "confidence": stt["confidence"],
        "duration_seconds": stt["duration_seconds"],
        **form,
        "summary_text": summary,
        "audio": tts,
        "timings_ms": {k: round(v) for k, v in timings.items()},
        "language_analysis": lang,
    })


@app.get("/")
def index():
    return send_from_directory(app.static_folder, "index.html")


@app.get("/voices")
def voices():
    return jsonify([{"key": k, "label": label, "voice_name": name}
                    for k, (label, name, _) in VOICES.items()])


@app.get("/telemetry-summary")
def telemetry_summary_endpoint():
    return jsonify(telemetry.telemetry_summary())


@app.get("/health")
def health():
    return jsonify({"status": "ok", "version": APP_VERSION,
                    "telemetry": telemetry.is_enabled()})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))  # macOS AirPlay occupies 5000
    app.run(host="0.0.0.0", port=port, debug=True)
