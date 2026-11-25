"""Part G — Application Insights (Azure Monitor OpenTelemetry).

init_telemetry() must run BEFORE Flask and the Azure SDKs are imported
(Pitfall 1), so app.py calls it right after load_dotenv().

G3  emit_pipeline_metrics(): per-stage custom metrics + the domain metric
G4  mark_pipeline_event():   pipeline_completed / pipeline_error as
                             attributes on the request span (requests table)
G8  log_pipeline_call() / telemetry_summary(): in-memory session log
"""
import os
import statistics
import threading

SERVICE_NAME = "voice-form-assistant"  # shows up as cloud_RoleName

_enabled = False


def init_telemetry():
    """Configure Azure Monitor. Returns True if telemetry is being exported.

    Skipped (no-op tracer/meter) when TELEMETRY_DISABLED=1 (offline tests) or
    when no connection string is set, so the app still runs without it.
    """
    global _enabled
    if os.environ.get("TELEMETRY_DISABLED") == "1":
        return False
    conn = os.environ.get("APPLICATIONINSIGHTS_CONNECTION_STRING")
    if not conn:
        print("WARNING: APPLICATIONINSIGHTS_CONNECTION_STRING not set; telemetry off.")
        return False
    os.environ.setdefault("OTEL_SERVICE_NAME", SERVICE_NAME)
    from azure.monitor.opentelemetry import configure_azure_monitor
    configure_azure_monitor(connection_string=conn)
    _enabled = True
    print("Application Insights initialized.")
    return True


def is_enabled():
    return _enabled


# --- G3: custom metrics -----------------------------------------------------
# Created lazily so they bind to the meter provider set up by init_telemetry().
_instruments = None
_SENTIMENT_SCORE = {"positive": 1.0, "neutral": 0.0, "negative": -1.0}


def _get_instruments():
    global _instruments
    if _instruments is None:
        from opentelemetry import metrics
        meter = metrics.get_meter(SERVICE_NAME)
        # Per-request values are histograms, not gauges: a gauge is re-exported
        # every 60 s with its last value, which fills customMetrics with
        # repeats and keeps the low-confidence alert firing with no traffic.
        g = h = meter.create_histogram
        _instruments = {
            "stt_confidence": g("stt_confidence"),
            "stt_duration_seconds": g("stt_duration_seconds"),
            "stt_word_count": g("stt_word_count"),
            "language_entity_count": g("language_entity_count"),
            "language_keyphrase_count": g("language_keyphrase_count"),
            "language_sentiment": g("language_sentiment"),
            "tts_char_count": g("tts_char_count"),
            "stage_stt_ms": h("stage_stt_ms", unit="ms"),
            "stage_language_ms": h("stage_language_ms", unit="ms"),
            "stage_tts_ms": h("stage_tts_ms", unit="ms"),
            # Domain metric (product brief §6)
            "fields_correct_first_try": g("fields_correct_first_try"),
            # Track 1: multi-turn correction
            "confirm_turns": meter.create_counter("confirm_turns"),
            "sessions_completed": meter.create_counter("sessions_completed"),
            "turns_to_complete": h("turns_to_complete"),
            "fields_corrected": h("fields_corrected"),
            # The domain metric measured from what the user actually corrected
            "fields_correct_first_try_confirmed": g("fields_correct_first_try_confirmed"),
            "stage_session_ms": h("stage_session_ms", unit="ms"),
            # Part H option 2: confidence-based retry
            "stt_confidence_primary": h("stt_confidence_primary"),
            "stt_retries": meter.create_counter("stt_retries"),
            "stt_low_confidence_words": h("stt_low_confidence_words"),
        }
    return _instruments


def sentiment_score(label):
    return _SENTIMENT_SCORE.get(label, 0.0)  # "mixed" counts as neutral


def emit_pipeline_metrics(stt, lang, tts, form, timings, audio_format):
    """Call once after all three stages of a successful request."""
    m = _get_instruments()
    attrs = {"audio_format": audio_format, "language": stt["language"],
             "voice": tts["voice"]}
    m["stt_confidence"].record(stt["confidence"], attrs)
    m["stt_duration_seconds"].record(stt["duration_seconds"], attrs)
    m["stt_word_count"].record(len(stt["transcript"].split()), attrs)
    m["language_entity_count"].record(len(lang["entities"]), attrs)
    m["language_keyphrase_count"].record(len(lang["key_phrases"]), attrs)
    m["language_sentiment"].record(sentiment_score(lang["sentiment"]["label"]), attrs)
    m["tts_char_count"].record(tts["char_count"], attrs)
    m["stage_stt_ms"].record(timings["stt_ms"], attrs)
    m["stage_language_ms"].record(timings["language_ms"], attrs)
    m["stage_tts_ms"].record(timings["tts_ms"], attrs)
    m["fields_correct_first_try"].record(form["fields_correct_first_try"], attrs)


def emit_retry_metrics(stt, audio_format):
    """Part H: first-attempt confidence, retry decisions and flagged words."""
    m = _get_instruments()
    r = stt["retry"]
    attrs = {"audio_format": audio_format}
    m["stt_confidence_primary"].record(r["primary_confidence"], attrs)
    m["stt_low_confidence_words"].record(len(stt["low_confidence_words"]), attrs)
    if r["retried"]:
        m["stt_retries"].add(1, {**attrs, "improved": r["improved"],
                                 "chosen_language": r["chosen_language"]})


def emit_confirm_metrics(session, action, timings):
    """Track 1: call once per successful /confirm turn."""
    m = _get_instruments()
    attrs = {"action": action, "status": session["status"]}
    m["confirm_turns"].add(1, attrs)
    for k in ("session_load_ms", "session_save_ms"):
        if k in timings:
            m["stage_session_ms"].record(timings[k], {"op": k.split("_")[1]})
    if action == "confirmed":
        m["sessions_completed"].add(1)
        m["turns_to_complete"].record(session["turn"])
        m["fields_corrected"].record(len(session["corrected_fields"]))
        m["fields_correct_first_try_confirmed"].record(session["fields_correct_first_try"])


def mark_confirm_event(span, session, action):
    span.set_attribute("event.name", "confirm_turn")
    span.set_attribute("dialogue.action", action)
    span.set_attribute("session.turn", session["turn"])
    span.set_attribute("session.status", session["status"])
    span.set_attribute("form.missing_fields", ",".join(session["missing_fields"]))
    span.set_attribute("form.low_confidence_fields",
                       ",".join(session["low_confidence_fields"]))
    span.set_attribute("form.corrected_fields", ",".join(session["corrected_fields"]))
    if session["fields_correct_first_try"] is not None:
        span.set_attribute("form.fields_correct_first_try_confirmed",
                           session["fields_correct_first_try"])


# --- G4: pipeline outcome on the request span ------------------------------
def mark_pipeline_event(span, audio_format, stt=None, lang=None, form=None,
                        error_stage=None, error=None):
    span.set_attribute("audio.format", audio_format)
    if error is None:
        span.set_attribute("event.name", "pipeline_completed")
        span.set_attribute("stt.confidence", stt["confidence"])
        span.set_attribute("stt.language", stt["language"])
        span.set_attribute("entities.count", len(lang["entities"]))
        span.set_attribute("sentiment", lang["sentiment"]["label"])
        span.set_attribute("form.fields_correct_first_try",
                           form["fields_correct_first_try"])
        span.set_attribute("form.missing_fields", ",".join(form["missing_fields"]))
        span.set_attribute("form.low_confidence_fields",
                           ",".join(form["low_confidence_fields"]))
    else:
        span.set_attribute("event.name", "pipeline_error")
        span.set_attribute("error.stage", error_stage)
        span.set_attribute("error.message", str(error))
        span.record_exception(error)


# --- G8: in-memory session log ---------------------------------------------
_session_log = []
_errors = []
_lock = threading.Lock()


def log_pipeline_call(stt, lang, form, timings, voice):
    with _lock:
        _session_log.append({
            "confidence": stt["confidence"],
            "language": stt["language"],
            "entity_count": len(lang["entities"]),
            "sentiment": lang["sentiment"]["label"],
            "fields_correct_first_try": form["fields_correct_first_try"],
            "missing_fields": form["missing_fields"],
            "low_confidence_fields": form["low_confidence_fields"],
            "voice": voice,
            **{k: round(v, 1) for k, v in timings.items()},
        })


def log_pipeline_error(stage):
    with _lock:
        _errors.append(stage)


def _p95(values):
    s = sorted(values)
    return round(s[max(0, int(len(s) * 0.95 + 0.5) - 1)], 1)


def telemetry_summary():
    with _lock:
        log, errors = list(_session_log), list(_errors)
    if not log:
        return {"message": "No calls yet", "errors": len(errors)}
    conf = [e["confidence"] for e in log]
    return {
        "total_calls": len(log) + len(errors),
        "successful_calls": len(log),
        "errors_by_stage": {s: errors.count(s) for s in sorted(set(errors))},
        "avg_confidence": round(statistics.mean(conf), 3),
        "min_confidence": round(min(conf), 3),
        "avg_fields_correct_first_try": round(
            statistics.mean(e["fields_correct_first_try"] for e in log), 3),
        "p95_stt_ms": _p95([e["stt_ms"] for e in log]),
        "p95_language_ms": _p95([e["language_ms"] for e in log]),
        "p95_tts_ms": _p95([e["tts_ms"] for e in log]),
        "sentiment_breakdown": {
            s: sum(1 for e in log if e["sentiment"] == s)
            for s in ["positive", "neutral", "negative", "mixed"]},
        "calls": log[-10:],
        "telemetry_exporting": _enabled,
    }
