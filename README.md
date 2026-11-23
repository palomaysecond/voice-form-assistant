# Voice Form Assistant

A voice-driven form filler for users who cannot easily type. The user speaks their answers; the app transcribes them with **Azure AI Speech**, extracts form fields (name, date, request type) with **Azure AI Language**, and reads back a confirmation.

CSC 645 · Cloud Computing Platforms & Services · Fall 2026 semester project.

## Architecture

```
audio (wav/mp3/webm/m4a) ─► /fill-form  (alias /process)
   span stage.speech_to_text     speech_service.transcribe     Azure Speech F0, continuous recognition
   span stage.language_analysis  language_service.analyze      Azure Language F0: NER, key phrases, sentiment
                                 form_extractor.extract_fields name / date / request_type, confidence = min(NLU, STT)
   span stage.text_to_speech     tts_service.synthesize        Neural TTS read-back, MP3 as Base64, 3 voices
   telemetry.py                  Application Insights: request trace, stage spans, custom metrics, pipeline event
```

**Domain metric `fields_correct_first_try`:** share of the 3 form fields accepted on the first pass (filled and not flagged for confirmation). Until `/confirm` exists it is the system's estimate; `/confirm` will subtract fields the user corrects.

## Endpoints

| Method | Path | Body | Returns |
|---|---|---|---|
| POST | `/transcribe` | multipart `audio` | transcript, language, duration, confidence, words |
| POST | `/analyze` | `{"text": "..."}` | entities, key_phrases, sentiment, fields |
| POST | `/fill-form` or `/process` | multipart `audio`, optional `voice` = formal / casual / lively | transcript, fields, missing / low-confidence fields, `fields_correct_first_try`, `summary_text`, `audio` (Base64 MP3), `timings_ms` |
| GET | `/voices` | — | the three TTS voices |
| GET | `/telemetry-summary` | — | in-memory stats: avg confidence, avg domain metric, p95 stage latency, errors by stage |
| GET | `/health` | — | status, whether telemetry is exporting |

Play the read-back in a browser with `audio.src = "data:audio/mp3;base64," + body.audio.audio_base64`.

Errors: 400 missing input · 413 > 25 MB · 415 unsupported audio · 422 no speech · 502 Azure error.

## Setup

Requires Python 3.11/3.12 and ffmpeg.

```bash
python3.12 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env   # fill in your own keys
.venv/bin/python -m pytest -q     # offline, telemetry disabled
.venv/bin/python app.py          # http://localhost:8000
curl -F audio=@sample.m4a -F voice=formal localhost:8000/process
```

## Provisioning (Azure CLI)

```bash
az group create --name cs345-speech-rg --location westus
az cognitiveservices account create --name cs345-speech-<id> -g cs345-speech-rg --kind SpeechServices --sku F0 --location westus --yes
az cognitiveservices account create --name cs345-language-<id> --custom-domain cs345-language-<id> -g cs345-speech-rg --kind TextAnalytics --sku F0 --location westus --yes
az monitor log-analytics workspace create -g cs345-speech-rg --workspace-name cs345-logs --location westus
az monitor app-insights component create --app cs345-insights -g cs345-speech-rg --location westus --workspace <WORKSPACE_ID>
```

## Part I extensions

- Track 1 — *(to be completed)* multi-turn voice follow-up for missing / low-confidence fields.
- Track 2 — *(to be completed)*.
