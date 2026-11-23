# KQL queries (Application Insights → Monitoring → Logs)

## One pipeline request and its three stage spans

```kusto
let op = toscalar(requests
    | where name has "process" or name has "fill-form"
    | top 1 by timestamp desc | project operation_Id);
union requests, dependencies
| where operation_Id == op
| project timestamp, itemType, name, duration_ms = round(duration), success,
          event = tostring(customDimensions["event.name"])
| order by timestamp asc
```

## Domain metric: fields_correct_first_try

Share of the three form fields (name, date, request type) accepted on the
first pass. Also on every request as `customDimensions["form.fields_correct_first_try"]`.

```kusto
customMetrics
| where name == "fields_correct_first_try"
| project timestamp, value, voice = tostring(customDimensions["voice"]),
          audio_format = tostring(customDimensions["audio_format"])
| order by timestamp desc
```

```kusto
requests
| where tostring(customDimensions["event.name"]) == "pipeline_completed"
| project timestamp, resultCode,
          fields_correct_first_try = todouble(customDimensions["form.fields_correct_first_try"]),
          low_confidence = tostring(customDimensions["form.low_confidence_fields"]),
          missing = tostring(customDimensions["form.missing_fields"]),
          stt_confidence = todouble(customDimensions["stt.confidence"])
| order by timestamp desc
```

## Stage latency and STT confidence (G6)

```kusto
// Histograms (stage_*_ms): each row's value is the SUM over one export
// interval, so the mean is sum(value) / sum(valueCount).
customMetrics
| where timestamp > ago(1h)
| where name in ("stage_stt_ms", "stage_language_ms", "stage_tts_ms")
| summarize avg_ms = round(sum(value) / sum(valueCount)), max_ms = round(max(valueMax)),
            calls = sum(valueCount) by name
| order by name asc
```

Gauges (`stt_confidence`, `fields_correct_first_try`, ...) are re-exported every
interval with their last value, so their row count is higher than the number of
calls. For exact per-call values use the `requests` query above.

## Failures by stage

```kusto
requests
| where tostring(customDimensions["event.name"]) == "pipeline_error"
| summarize count() by stage = tostring(customDimensions["error.stage"]), resultCode
```
