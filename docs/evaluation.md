# Operational evaluation

`backend/app/evaluation.py` collects measurements from normal application use. It does not send evaluation prompts, generate images, retry requests, or replay traffic. No golden dataset is required for these operational measurements. They do not establish search relevance, classification accuracy, identity preservation or image quality.

## Collect a run

Add the following non-secret settings to `backend/.env`, then restart the backend:

```dotenv
EVALUATION_ENABLED=1
EVALUATION_RUN=hackathon-demo-01
```

Use the app normally. New measurements are appended to `backend/data/evaluation.csv` by default, including in a Docker data volume. Recording starts after the instrumented backend starts; previous requests cannot be reconstructed. Set `EVALUATION_FILE` to an absolute path to change the destination. Set `EVALUATION_ENABLED=0` to stop writing metrics. No extra model calls are made by measurement itself; normal app actions can still trigger its existing paid requests.

Each record has an event ID, UTC timestamp, run label, configured catalogue mode, operation, outcome, selected shopping department (`male`, `female` or `null` when unknown), and relevant numeric measurements. Department comes from the explicit shopping selection, with the saved photo preference taking precedence; it is not inferred from the photo. Background jobs snapshot that preference so later changes do not relabel an earlier job. API responses include `X-Request-ID`. Provider calls and background generation jobs inherit their originating request ID; jobs additionally carry batch/job IDs. A manual retry gets a new job ID with `retry_of` linking the original job.

The evaluation file is append-only and local. CSV records use one row per event, with separate columns for timings, department and token totals. Unknown values are blank; reported zeros remain zero. Nested provider usage details are preserved in a `usage_json` cell. It has no public API endpoint. It survives backend restarts independently of in-memory sessions and jobs. One backend worker remains the supported deployment configuration. File rotation and retention are not implemented; archive or change the output path between long-running experiments.

## Generate a report

From `backend`, with the virtual environment activated:

```sh
python -m app.evaluation --run hackathon-demo-01 --output data/evaluation-summary.csv
```

To print the report in the terminal:

```sh
python -m app.evaluation --run hackathon-demo-01
```

For an archived file:

```sh
python -m app.evaluation --input /absolute/path/events.jsonl --run hackathon-demo-01 --output data/evaluation-summary.csv
```

These commands only read measurements and write/print a report. They make no network requests. A missing input file produces an empty report; invalid records are counted and skipped. CSV summaries use one row per operation/department group, with flattened columns such as `latency_ms.p95` and `tokens.total.sum`. JSON summary output is still supported by choosing a `.json` output filename. Use the same run label set in the backend environment. Without `--run`, records from all labels are included. By default, the report reads the new CSV recording and any older `backend/data/evaluation.jsonl` file, deduplicating matching event IDs. An explicit `--input` or `EVALUATION_FILE` selects that file only. Existing JSONL files are not rewritten. A custom `EVALUATION_FILE` ending in `.jsonl` retains JSONL event storage; use `.csv` for CSV storage. Use distinct files/labels for mock, manual, and live experiments.

To compare departments separately:

```sh
python -m app.evaluation --department male --output data/evaluation-men.csv
python -m app.evaluation --department female --output data/evaluation-women.csv
```

`--department unknown` selects older/unscoped records. Department filters can be combined with `--run`.

To export individual historical records to CSV as well as a summary:

```sh
python -m app.evaluation --input data/evaluation.jsonl --export-events data/evaluation-events.csv --output data/evaluation-summary.csv
```

Use a separate export path rather than overwriting a recording file while the backend is writing it. All exports are local; they do not run model requests.

## What is measured

| Population / operation | Measurement |
| --- | --- |
| `provider_request`: `shopify.search_catalog`, `shopify.lookup_catalog`, `shopify.get_product` | HTTP-attempt latency and HTTP/JSON-RPC/UCP outcome |
| `provider_request`: `chat` | Muse Spark HTTP-attempt latency and reported usage |
| `provider_request`: `photo_assessment` | Muse Spark vision HTTP-attempt latency and reported usage |
| `provider_request`: `garment_classification` | Batched Muse Spark classification HTTP-attempt latency and reported usage |
| `provider_request`: `image_generation` | Muse Image HTTP-attempt latency, reported usage and returned-image count |
| `api_request`: `api.request` | Response-body completion time for search, products, chat, upload/delete, and batch endpoints; excludes later background execution |
| `job`: `preview.job` | Eligible queued-job execution outcomes, manual retry count, queue wait and total duration including queue wait |
| `batch`: `preview.batch` | Requested, newly scheduled, reused, blocked and unavailable product counts for each submission |
| `batch_timing`: `preview.batch` | Server-side time to first available preview and terminal batch duration |
| `cache` | Photo-assessment, garment-classification and preview-job cache hit/miss observations |
| `decision` | Photo-assessment and garment-classification outcomes, separate from provider transport outcomes |

The report groups records by population, operation, provider, model, catalogue mode and selected shopping department. Older records without a department remain in the null/unknown group; they are not backfilled. It includes count/outcome breakdowns, mean/p50/p95 latency with sample counts, known token totals with reporting coverage, cache hits/misses and retries. Cache observations and batch product counts describe submissions, not globally unique products. `new_generations` counts jobs scheduled; actual Meta attempts are the `image_generation` provider records.

HTTP success means an HTTP response/protocol succeeded; it does not mean a model output was correct, a photo was usable or a preview passed visual review. JSON parsing and application/job outcomes are observable separately. A single user request can produce several provider records; sum provider rows for external-call counts, not all records together. The collector does not invent an automatic retry or implement provider idempotency.

Polling `GET /api/try-on/{id}`, session checks, health checks and CORS preflights are excluded from API response counts. They do not inflate model-call counts. Provider calls each generate exactly one measurement, including connection failures/timeouts. No usage can be recovered from a response that never arrived.

### Token and image usage

The collector stores allowlisted numeric fields from the provider's `usage` object. Chat Completions `prompt_tokens` / `completion_tokens` and `input_tokens` / `output_tokens` normalize to report input/output totals. Reported `total_tokens` is retained; if absent, a total is derived only when both input and output counts are supplied. Missing usage is `null` / unknown, never assumed to be zero. Coverage counts show how many requests actually supplied the values.

Cached and reasoning token details remain in raw records as subsets of the provider totals. Do not add them to the totals again. Some image calls may report tokens, image units, both or neither. Returned image counts are not a cost calculation. Actual charges require the applicable model billing units/rates and provider billing records; the report deliberately does not invent prices or assume failed calls are free.

### Preview timing limits

`preview.job` latency includes time waiting for one of the three generation slots. Provider image latency describes only its HTTP request, not that queue wait. First-preview timing starts at backend batch submission, not when the user starts typing or uploading. Already completed cached images have first-preview availability of zero. It does not measure image download, browser rendering or visual correctness.

A batch snapshot is `finished` only if all of its observed jobs are terminal. A submission reusing jobs still running from an earlier batch is marked `pending`; it has no terminal duration sample. The originating batch records its own completion. Pending/crashed/in-flight runs are not treated as completed, and `finished` includes terminal failures/skips as well as successes. Check completed/failed/blocked counts before interpreting batch latency.

## Privacy and verification

Evaluation records exclude prompts, user messages, raw model output, image bytes/base64, signed image URLs, photo hashes, session bearer tokens and credentials. Only allowlisted numeric usage and generated correlation IDs are written. Existing application debug logs are separate from this collector. Telemetry write failures log a warning and do not fail a shopping request.

Tests isolate all measurements in temporary files labelled `test`. They use mocked provider responses; no paid requests are issued. Tests verify usage extraction, missing usage, failure recording without retries, context isolation, response-vs-background timing, polling exclusion, report aggregation, cache/retry correlation and safe telemetry failures. Live model accuracy and generated-image quality still require separate evaluation; do not present these test results as real model performance.

### Preview feedback

Completed previews have a heart (like), thumbs-down (dislike), and comment icon with preset reasons. Wishlist uses a star. Each successful feedback submission appends a row to `DATA_DIR/feedback.csv` (normally `backend/data/feedback.csv`) with a UTC timestamp, hashed session reference, job ID, product ID, rating, optional reason, generated-image URL and product URL. Existing CSV files are upgraded automatically; older rows have blank URL fields. Images are not downloaded, and hosted image links may expire. Changes append another row; they do not overwrite earlier feedback. No database or model call is involved. The card and zoom popup share the selection until the page is refreshed; it does not reload previous ratings after a refresh. This CSV is separate from the operational evaluation report.
