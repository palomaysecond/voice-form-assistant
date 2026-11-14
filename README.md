# Voice Form Assistant

A voice-driven form filler for users who cannot easily type. The user speaks their answers; the app transcribes them with **Azure AI Speech**, extracts form fields (name, date, request type) with **Azure AI Language**, and reads back a confirmation.

CSC 645 · Cloud Computing Platforms & Services · Fall 2026 semester project.

## Architecture

```
audio (wav/mp3/webm) ─► /fill-form
   1. speech_service.transcribe     Azure Speech F0, continuous recognition
   2. language_service.analyze      Azure Language F0: NER, key phrases, sentiment
   3. form_extractor.extract_fields name / date / request_type + missing fields
   4. (Part D) Neural TTS confirmation   5. (Part G) Application Insights telemetry
```

## Endpoints (checkpoint)

| Method | Path | Body | Returns |
|---|---|---|---|
| POST | `/transcribe` | multipart `audio` | transcript, language, duration, confidence, words |
| POST | `/analyze` | `{"text": "..."}` | entities, key_phrases, sentiment, fields |
| POST | `/fill-form` | multipart `audio` | transcript, fields, missing_fields, confirmation_text |
| GET | `/health` | — | status |

Errors: 400 missing input · 413 > 25 MB · 415 unsupported audio · 422 no speech · 502 Azure error.

## Setup

Requires Python 3.11/3.12 and ffmpeg.

```bash
python3.12 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env   # fill in your own keys
.venv/bin/python -m pytest -q
.venv/bin/python app.py          # http://localhost:8000
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
