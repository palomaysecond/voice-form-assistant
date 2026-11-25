# Voice Form Assistant

A voice-driven form filler for users who cannot easily type. The user speaks their answers; the app transcribes them with **Azure AI Speech**, extracts form fields (name, date, request type) with **Azure AI Language**, and reads back a confirmation.

CSC 645 · Cloud Computing Platforms & Services · Fall 2026 semester project.

**Live:** https://cs345-speech-xionj226.azurewebsites.net (Azure App Service, Linux, F1, westus). Open it to record or upload a request.

## Architecture

```
audio (wav/mp3/webm/m4a) ─► /fill-form  (alias /process)
   span stage.speech_to_text     speech_service.transcribe     Azure Speech F0, continuous recognition
   span stage.language_analysis  language_service.analyze      Azure Language F0: NER, key phrases, sentiment
                                 form_extractor.extract_fields name / date / request_type, confidence = min(NLU, STT)
   span stage.text_to_speech     tts_service.synthesize        Neural TTS read-back, MP3 as Base64, 3 voices
   span stage.session_save       dialogue.new_session          Track 1: session opened in Cosmos DB, session_id returned
   telemetry.py                  Application Insights: request trace, stage spans, custom metrics, pipeline event

reply (audio or text) + session_id ─► /confirm                    Track 1: one correction turn
   span stage.session_load       session_store (Cosmos DB free tier, 7-day TTL)
   span stage.speech_to_text     same STT, word list keeps spelled letters ("J A C K")
   span stage.language_analysis  dialogue.apply_reply          spelled name / "no, Tuesday" / missing field / yes / no
   span stage.session_save       updated session written back
   span stage.text_to_speech     next question, or "Your request is confirmed"
```

**Domain metric `fields_correct_first_try`:** share of the 3 form fields the user accepts at the first read-back without correcting them. `/fill-form` reports the system's estimate (filled and not flagged). When the user confirms a session, `/confirm` reports the real value: fields that were missing or that the user corrected do not count (`fields_correct_first_try_confirmed` in Application Insights).

## Endpoints

| Method | Path | Body | Returns |
|---|---|---|---|
| POST | `/transcribe` | multipart `audio` | transcript, language, duration, confidence, words |
| POST | `/analyze` | `{"text": "..."}` | entities, key_phrases, sentiment, fields |
| POST | `/fill-form` or `/process` | multipart `audio`, optional `voice` = formal / casual / lively | transcript, fields, missing / low-confidence fields, `fields_correct_first_try`, `summary_text`, `audio` (Base64 MP3), `timings_ms` |
| POST | `/confirm` | `session_id` + multipart `audio`, or `text` (form or JSON), optional `voice` | updated `fields`, `missing_fields`, `low_confidence_fields`, `corrected_fields`, `status`, `complete`, next `confirmation_text` + `audio`, `fields_correct_first_try` once confirmed |
| GET | `/sessions/<id>` | — | the stored session (status, fields, turn history) |
| GET | `/voices` | — | the three TTS voices |
| GET | `/telemetry-summary` | — | in-memory stats: avg confidence, avg domain metric, p95 stage latency, errors by stage |
| GET | `/health` | — | status, app version, whether telemetry is exporting |

Play the read-back in a browser with `audio.src = "data:audio/mp3;base64," + body.audio.audio_base64`.

`/fill-form` also returns `session_id`. Example correction loop:

```bash
SID=$(curl -s -F audio=@t1.m4a localhost:8000/fill-form | jq -r .session_id)
curl -F session_id=$SID -F audio=@spelled_name.wav localhost:8000/confirm   # "J, A, C, K ..."
curl -H 'Content-Type: application/json' -d "{\"session_id\":\"$SID\",\"text\":\"yes\"}" localhost:8000/confirm
```

Errors: 400 missing input · 404 unknown/expired session · 413 > 25 MB · 415 unsupported audio · 422 no speech · 502 Azure error.

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
# Track 1: Cosmos DB free tier (one per subscription), shared 400 RU/s, sessions expire after 7 days
az provider register --namespace Microsoft.DocumentDB --wait
az cosmosdb create -g cs345-speech-rg -n cs345-cosmos-<id> --enable-free-tier true \
  --locations regionName=westus failoverPriority=0 isZoneRedundant=False
az cosmosdb sql database create -g cs345-speech-rg -a cs345-cosmos-<id> -n voiceform --throughput 400
az cosmosdb sql container create -g cs345-speech-rg -a cs345-cosmos-<id> -d voiceform -n sessions \
  --partition-key-path /id --ttl 604800
```

## Deployment (Azure App Service)

```bash
az appservice plan create -n cs345-speech-plan -g cs345-speech-rg --location westus --sku F1 --is-linux
az webapp create -g cs345-speech-rg -p cs345-speech-plan -n cs345-speech-xionj226 --runtime "PYTHON:3.12"
az webapp config appsettings set -g cs345-speech-rg -n cs345-speech-xionj226 --settings \
  AZURE_SPEECH_KEY=... AZURE_SPEECH_REGION=westus AZURE_LANGUAGE_KEY=... AZURE_LANGUAGE_ENDPOINT=... \
  APPLICATIONINSIGHTS_CONNECTION_STRING=... SCM_DO_BUILD_DURING_DEPLOYMENT=true OTEL_SERVICE_NAME=voice-form-assistant
az webapp config set -g cs345-speech-rg -n cs345-speech-xionj226 --startup-file startup.sh
az webapp update -g cs345-speech-rg -n cs345-speech-xionj226 --https-only true
git archive --format=zip -o app.zip HEAD   # committed files only: no .env, no .venv
az webapp deploy -g cs345-speech-rg -n cs345-speech-xionj226 --src-path app.zip --type zip
```

`startup.sh` installs ffmpeg (not in the App Service Python image) and starts gunicorn. Secrets live only in App Settings.

## Part I extensions

- Track 1 — **confidence-aware multi-turn correction** (`dialogue.py`, `session_store.py`, `POST /confirm`). The first pass flags fields where Language or Speech is unsure, for example a name that NER scores 1.0 but Speech heard with 0.05 confidence. The app then asks only about those fields and the missing ones. It accepts a spelled name ("J, A, C, K", detected from the single letters in the Speech word list), a correction ("no, Tuesday"), a missing value, or "yes". It finishes only after the user says yes to a read-back. Session state lives in Azure Cosmos DB (free tier, 7-day TTL). The page has a reply recorder and a typed-reply box.
- Track 2 — **CI/CD with GitHub Actions + OpenID Connect** ([`.github/workflows/ci-cd.yml`](.github/workflows/ci-cd.yml)).
  Every push and pull request runs the offline tests. A push to `main` that passes them is deployed to App Service with the same zip deploy as above, and the job then waits until the live `/health` reports the new `APP_VERSION`. The job signs in to Azure with a federated credential (no password or publish profile in GitHub). That credential only trusts the `production` environment of this repository, and its service principal has *Website Contributor* on this one web app.

  One-time setup:

  ```bash
  az ad app create --display-name voice-form-assistant-github          # note appId and id
  az ad sp create --id <appId>
  az ad app federated-credential create --id <id> --parameters '{"name":"github-production-env",
    "issuer":"https://token.actions.githubusercontent.com",
    "subject":"repo:palomaysecond/voice-form-assistant:environment:production",
    "audiences":["api://AzureADTokenExchange"]}'
  az role assignment create --assignee <appId> --role "Website Contributor" \
    --scope $(az webapp show -g cs345-speech-rg -n cs345-speech-xionj226 --query id -o tsv)
  gh secret set AZURE_CLIENT_ID -b <appId>
  gh secret set AZURE_TENANT_ID -b $(az account show --query tenantId -o tsv)
  gh secret set AZURE_SUBSCRIPTION_ID -b $(az account show --query id -o tsv)
  ```
