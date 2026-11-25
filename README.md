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
| GET | `/health` | — | status, app version, whether telemetry is exporting |

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

- Track 1 — *(to be completed)* multi-turn voice follow-up for missing / low-confidence fields.
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
