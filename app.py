"""Voice Form Assistant — Flask entry point (App Service expects app.py).

  POST /transcribe          audio -> transcript                      (Part B)
  POST /analyze             {"text"} -> entities + fields             (Part C)
  POST /fill-form, /process audio -> STT -> Language -> TTS read-back (C2 + D)
  POST /confirm             session_id + spoken (or typed) reply ->
                            updated form, next question     (Track 1)
  GET  /sessions/<id>       stored session state (Cosmos DB) (Track 1)
  GET  /voices              the three TTS voices                     (D3)
  GET  /telemetry-summary   in-memory pipeline stats                 (G8)
  GET  /stats               SQLite confidence + retry summary  (H option 2)
  GET  /                    record / upload page with results         (E)
  GET  /health
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

import stats_store
import telemetry
from confidence_retry import transcribe_with_retry
from dialogue import apply_reply, new_session
from form_extractor import extract_fields
from language_service import LanguageServiceError, analyze
from speech_service import SpeechServiceError, UnsupportedAudioError, transcribe
from session_store import SessionStoreError, get_store
from tts_service import VOICES, build_summary, resolve_voice, synthesize

ALLOWED_EXTENSIONS = {"wav", "mp3", "webm", "ogg", "m4a"}
TEMP_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "temp_audio")
os.makedirs(TEMP_DIR, exist_ok=True)

tracer = trace.get_tracer(telemetry.SERVICE_NAME)

# Bump on every release; the CI/CD workflow's deploy is verified by reading it back.
APP_VERSION = "1.4.0"

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
@app.errorhandler(SessionStoreError)
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

            # Stage 1: Speech-to-Text, retried with en-GB below 0.85 (Part H)
            with tracer.start_as_current_span("stage.speech_to_text") as span:
                t0 = time.perf_counter()
                try:
                    stt = transcribe_with_retry(
                        transcribe, path,
                        retry_span=lambda: tracer.start_as_current_span("stage.stt_retry"))
                finally:
                    os.remove(path)
                stt_ms = _ms_since(t0)
                span.set_attribute("stt.confidence", stt["confidence"])
                span.set_attribute("stt.primary_confidence", stt["retry"]["primary_confidence"])
                span.set_attribute("stt.retried", stt["retry"]["retried"])
                span.set_attribute("stt.language", stt["language"])
                span.set_attribute("stt.low_confidence_words", len(stt["low_confidence_words"]))
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

            # Track 1: open a multi-turn session so /confirm can fix fields.
            session_id = _open_session(form, voice)
            root.set_attribute("session.id", session_id or "")
            _log_confidence(stt, audio_format, session_id)

            timings = {"stt_ms": stt_ms, "language_ms": lang_ms, "tts_ms": tts_ms}
            telemetry.emit_pipeline_metrics(stt, lang, tts, form, timings, audio_format)
            telemetry.emit_retry_metrics(stt, audio_format)
            telemetry.mark_pipeline_event(request_span, audio_format,
                                          stt=stt, lang=lang, form=form)
            telemetry.log_pipeline_call(stt, lang, form, timings, voice)
    except Exception as e:
        telemetry.mark_pipeline_event(request_span, audio_format,
                                      error_stage=stage, error=e)
        telemetry.log_pipeline_error(stage)
        raise  # the error handlers above turn it into 415/422/502

    return jsonify({
        "session_id": session_id,
        "transcript": stt["transcript"],
        "confidence": stt["confidence"],
        "duration_seconds": stt["duration_seconds"],
        "language": stt["language"],
        "stt_retry": stt["retry"],
        "low_confidence_words": stt["low_confidence_words"],
        **form,
        "summary_text": summary,
        "audio": tts,
        "timings_ms": {k: round(v) for k, v in timings.items()},
        "language_analysis": lang,
    })


def _log_confidence(stt, audio_format, session_id):
    """Part H: write the confidences and retry decision to SQLite. A storage
    failure is recorded on the span but never fails the request."""
    with tracer.start_as_current_span("stage.stats_log") as span:
        try:
            stats_store.record(stt, "/process", audio_format, session_id)
            span.set_attribute("stats.saved", True)
        except stats_store.StatsStoreError as e:
            span.record_exception(e)
            span.set_attribute("stats.saved", False)


def _open_session(form, voice):
    """Save the first-pass form as a session. A storage failure must not
    break the main pipeline: the user just can't use /confirm then."""
    with tracer.start_as_current_span("stage.session_save") as span:
        session = new_session(form, voice)
        try:
            get_store().save(session)
        except SessionStoreError as e:
            span.record_exception(e)
            span.set_attribute("session.saved", False)
            return None
        span.set_attribute("session.store", get_store().kind)
        span.set_attribute("session.saved", True)
        return session["id"]


def _public_session(session):
    keys = ["status", "turn", "fields", "missing_fields", "low_confidence_fields",
            "corrected_fields", "fields_correct_first_try", "first_try_estimate",
            "confirmation_text", "history", "created_at", "updated_at"]
    return {"session_id": session["id"], "complete": session["status"] == "complete",
            **{k: session[k] for k in keys}}


@app.post("/confirm")
def confirm_endpoint():
    """Track 1: one correction turn.

    multipart: session_id + audio (+ optional voice), or
    multipart / JSON: session_id + text (typed reply, for testing and for
    users who cannot speak at that moment).
    """
    body = request.get_json(silent=True) or {}
    session_id = (request.form.get("session_id") or body.get("session_id") or "").strip()
    if not session_id:
        return _error(400, "Send 'session_id' from the /fill-form response.")
    has_audio = "audio" in request.files
    text = (request.form.get("text") or body.get("text") or "").strip()
    if not has_audio and not text:
        return _error(400, "Send the reply as multipart field 'audio' or as 'text'.")

    request_span = trace.get_current_span()
    request_span.set_attribute("session.id", session_id)
    stage = "session_load"
    timings = {}
    try:
        with tracer.start_as_current_span("pipeline.confirm") as root:
            with tracer.start_as_current_span("stage.session_load") as span:
                t0 = time.perf_counter()
                session = get_store().get(session_id)
                timings["session_load_ms"] = _ms_since(t0)
                span.set_attribute("session.store", get_store().kind)
            if session is None:
                return _error(404, "Unknown or expired session_id.")
            try:
                voice = resolve_voice(request.form.get("voice") or body.get("voice")
                                      or session.get("voice"))
            except ValueError as e:
                return _error(400, str(e))

            words = None
            if has_audio:
                stage = "speech_to_text"
                path, _, err = _save_upload()
                if err:
                    return err
                with tracer.start_as_current_span("stage.speech_to_text") as span:
                    t0 = time.perf_counter()
                    try:
                        stt = transcribe(path)
                    finally:
                        os.remove(path)
                    timings["stt_ms"] = _ms_since(t0)
                    span.set_attribute("stt.confidence", stt["confidence"])
                text, words = stt["transcript"], stt["words"]
                if not text:
                    raise NoSpeechError("No speech was recognized in the reply.")
            root.set_attribute("reply.mode", "audio" if has_audio else "text")

            stage = "language_analysis"
            with tracer.start_as_current_span("stage.language_analysis") as span:
                t0 = time.perf_counter()
                lang = analyze(text)
                session, action = apply_reply(session, text, lang["entities"], words)
                timings["language_ms"] = _ms_since(t0)
                span.set_attribute("dialogue.action", action)
                span.set_attribute("entity_count", len(lang["entities"]))

            stage = "session_save"
            with tracer.start_as_current_span("stage.session_save") as span:
                t0 = time.perf_counter()
                get_store().save(session)
                timings["session_save_ms"] = _ms_since(t0)
                span.set_attribute("session.status", session["status"])

            stage = "text_to_speech"
            with tracer.start_as_current_span("stage.text_to_speech") as span:
                t0 = time.perf_counter()
                tts = synthesize(session["confirmation_text"], voice)
                timings["tts_ms"] = _ms_since(t0)
                span.set_attribute("char_count", tts["char_count"])

            telemetry.emit_confirm_metrics(session, action, timings)
            telemetry.mark_confirm_event(request_span, session, action)
    except Exception as e:
        telemetry.mark_pipeline_event(request_span, "confirm",
                                      error_stage=stage, error=e)
        telemetry.log_pipeline_error(stage)
        raise

    return jsonify({
        **_public_session(session),
        "action": action,
        "transcript": text,
        "audio": tts,
        "timings_ms": {k: round(v) for k, v in timings.items()},
    })


@app.get("/sessions/<session_id>")
def get_session(session_id):
    session = get_store().get(session_id)
    if session is None:
        return _error(404, "Unknown or expired session_id.")
    return jsonify(_public_session(session))


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


@app.get("/stats")
def stats_endpoint():
    """Part H: confidence and retry summary of every logged transcription."""
    try:
        recent = max(0, min(int(request.args.get("recent", 5)), 50))
    except ValueError:
        return _error(400, "'recent' must be an integer.")
    try:
        return jsonify(stats_store.summary(recent=recent))
    except stats_store.StatsStoreError as e:
        return _error(503, str(e))


@app.get("/health")
def health():
    return jsonify({"status": "ok", "version": APP_VERSION,
                    "telemetry": telemetry.is_enabled(),
                    "session_store": get_store().kind})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))  # macOS AirPlay occupies 5000
    app.run(host="0.0.0.0", port=port, debug=True)
