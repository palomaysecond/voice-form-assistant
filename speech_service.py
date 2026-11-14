"""Part B — Speech-to-Text with Azure AI Speech (continuous recognition).

Flow: uploaded file -> (ffmpeg) 16 kHz mono 16-bit WAV -> SpeechRecognizer
with continuous recognition -> collect every `recognized` segment -> JSON.
"""
import json
import os
import shutil
import subprocess
import threading
import wave

import azure.cognitiveservices.speech as speechsdk

TICKS_PER_SECOND = 10_000_000  # Speech SDK offsets/durations are in 100-ns ticks


class UnsupportedAudioError(Exception):
    """The audio could not be decoded -> the API answers HTTP 415."""


class SpeechServiceError(Exception):
    """Azure Speech returned an error (bad key, quota, network) -> HTTP 502."""


def _is_pcm_wav_16k_mono(path):
    try:
        with wave.open(path, "rb") as w:
            return (w.getframerate() == 16000 and w.getnchannels() == 1
                    and w.getsampwidth() == 2)
    except (wave.Error, EOFError):
        return False


def to_wav_16k_mono(src_path):
    """Return a path to a 16 kHz mono 16-bit PCM WAV version of src_path.

    Browser recordings (WebM/Opus), MP3 and M4A are converted with ffmpeg.
    Raises UnsupportedAudioError if conversion is impossible.
    """
    if _is_pcm_wav_16k_mono(src_path):
        return src_path
    if shutil.which("ffmpeg") is None:
        raise UnsupportedAudioError(
            "Audio is not 16 kHz mono WAV and ffmpeg is not installed to convert it.")
    dst_path = os.path.splitext(src_path)[0] + "_16k.wav"
    proc = subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-i", src_path,
         "-ac", "1", "-ar", "16000", "-sample_fmt", "s16", dst_path],
        capture_output=True, text=True)
    if proc.returncode != 0 or not os.path.exists(dst_path):
        # Don't echo ffmpeg's stderr: it contains server file paths.
        raise UnsupportedAudioError(
            "Could not decode the audio file. Upload a valid WAV, MP3, WebM, OGG or M4A recording.")
    return dst_path


def _wav_duration_seconds(path):
    with wave.open(path, "rb") as w:
        return w.getnframes() / float(w.getframerate())


def transcribe(audio_path, language="en-US", timeout_s=120):
    """Transcribe a whole audio file. Returns the Part B response dict.

    Uses start_continuous_recognition (not recognize_once, which stops at the
    first pause / ~30 s). The SDK is event driven, so we block on a
    threading.Event until `session_stopped` or `canceled` fires (Pitfall 3).
    """
    wav_path = to_wav_16k_mono(audio_path)
    try:
        speech_config = speechsdk.SpeechConfig(
            subscription=os.environ["AZURE_SPEECH_KEY"],
            region=os.environ["AZURE_SPEECH_REGION"])
        speech_config.speech_recognition_language = language
        speech_config.output_format = speechsdk.OutputFormat.Detailed
        speech_config.request_word_level_timestamps()

        audio_config = speechsdk.audio.AudioConfig(filename=wav_path)
        recognizer = speechsdk.SpeechRecognizer(
            speech_config=speech_config, audio_config=audio_config)

        segments, words, errors = [], [], []
        done = threading.Event()

        def on_recognized(evt):
            res = evt.result
            if res.reason != speechsdk.ResultReason.RecognizedSpeech or not res.text:
                return
            best = json.loads(res.json)["NBest"][0]
            segments.append({"text": res.text,
                             "confidence": best.get("Confidence", 0.0),
                             "duration": res.duration / TICKS_PER_SECOND})
            for w in best.get("Words", []):
                words.append({
                    "word": w["Word"],
                    "offset": round(w["Offset"] / TICKS_PER_SECOND, 2),
                    "duration": round(w["Duration"] / TICKS_PER_SECOND, 2),
                    # Word-level confidence is not always returned.
                    "confidence": w.get("Confidence"),
                })

        def on_canceled(evt):
            d = evt.cancellation_details
            if d.reason == speechsdk.CancellationReason.Error:
                errors.append((d.error_code, d.error_details))
            done.set()  # EndOfStream also arrives here

        recognizer.recognized.connect(on_recognized)
        recognizer.canceled.connect(on_canceled)
        recognizer.session_stopped.connect(lambda evt: done.set())

        recognizer.start_continuous_recognition_async().get()
        finished = done.wait(timeout=timeout_s)
        recognizer.stop_continuous_recognition_async().get()

        if errors:
            code, details = errors[0]
            if "header" in (details or "").lower() or "format" in (details or "").lower():
                raise UnsupportedAudioError(f"Speech SDK rejected the audio: {details}")
            raise SpeechServiceError(f"Azure Speech error ({code}): {details}")
        if not finished:
            raise SpeechServiceError("Transcription timed out.")

        # Overall confidence = segment confidences weighted by segment length.
        total = sum(s["duration"] for s in segments)
        if segments and total > 0:
            confidence = sum(s["confidence"] * s["duration"] for s in segments) / total
        elif segments:
            confidence = sum(s["confidence"] for s in segments) / len(segments)
        else:
            confidence = 0.0

        return {
            "transcript": " ".join(s["text"] for s in segments),
            "language": language,
            "duration_seconds": round(_wav_duration_seconds(wav_path), 2),
            "confidence": round(confidence, 3),
            "words": words,
        }
    finally:
        if wav_path != audio_path and os.path.exists(wav_path):
            os.remove(wav_path)
