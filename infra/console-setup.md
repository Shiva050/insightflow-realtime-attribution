# Infrastructure — console setup record

We are building console-first. This file is the record of what was clicked, so
the infrastructure can be codified later by transcription rather than
archaeology. **Update it as you click.** An undocumented console resource is a
resource nobody can rebuild.

Region for everything: **us-east-1** (the public source buckets — `dea-lead-owner`
and `dea-data-bucket` — are us-east-1; staying local avoids cross-region
transfer and latency).

Status legend: `[ ]` not created · `[x]` created and verified

> **Deployed 2026-07-26** into account `995679261492`, us-east-1. Everything
> below exists and has been exercised against live data. Built by CLI rather
> than clicked, so the commands are the record; this file remains the
> description of intent and reasoning.
>
> **Live webhook endpoints:**
> ```
> POST https://wx13s08t9k.execute-api.us-east-1.amazonaws.com/deploy/crm
> POST https://wx13s08t9k.execute-api.us-east-1.amazonaws.com/deploy/calendly
> ```
>
> Still outstanding: `SLACK_WEBHOOK_URL` is unset on `insightflow-crm-enrich`
> and `insightflow-owner-sweep`, so alerts are logged to CloudWatch rather than
> posted. The signing keys are also unset, so webhook signatures are accepted
> unverified — set both before the seven-day evaluation window starts.

---

## 1. S3 — Bronze bucket

- [x] `insightflow-bronze` (created via console)

Prefix layout — one folder per source:

```
insightflow-bronze/
├── crm/
│   └── events/dt=YYYY-MM-DD/crm_event_{event_id}.json
├── calendly/
│   ├── bookings/dt=YYYY-MM-DD/calendly_event_{invitee_uuid}.json
│   ├── cancellations/dt=YYYY-MM-DD/calendly_cancel_{invitee_uuid}.json
│   └── spend/dt=YYYY-MM-DD/spend_data_YYYY-MM-DD.json
├── wistia/
│   ├── media/hashed_id={id}/media.json
│   ├── media_stats/dt=YYYY-MM-DD/{hashed_id}.json
│   └── visitor_events/dt=YYYY-MM-DD/{event_key}.json
└── seeds/
    ├── channel_map.json
    └── custom_field_map.json
```

`dt=` is derived from the **payload's own event time**, never wall-clock. This
keeps the object key a pure function of the payload, so a webhook retry
regenerates the identical key and overwrites itself instead of landing a
duplicate under a different date (S2 — event time, not processing time).

Recommended settings:
- [ ] Block all public access: **ON** (this is our bucket; the *sources* are public, we are not)
- [ ] Default encryption: SSE-S3
- [ ] Versioning: **ON** — cheap insurance while Bronze semantics are still settling

---

## 2. IAM — execution role for the ingest Lambdas

- [ ] Role name: `insightflow-ingest-lambda-role`
- [ ] Trust: `lambda.amazonaws.com`
- [ ] Attach managed policy: `AWSLambdaBasicExecutionRole` (CloudWatch Logs)
- [ ] Inline policy, write-only to the two ingest prefixes:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "BronzeIngestWrite",
      "Effect": "Allow",
      "Action": "s3:PutObject",
      "Resource": [
        "arn:aws:s3:::insightflow-bronze/crm/events/*",
        "arn:aws:s3:::insightflow-bronze/calendly/bookings/*",
        "arn:aws:s3:::insightflow-bronze/calendly/cancellations/*"
      ]
    }
  ]
}
```

Deliberately narrow: the ingest functions can write their own prefix and nothing
else. No `s3:GetObject`, no `s3:DeleteObject` — Bronze is append-and-overwrite
only, and a public endpoint should not hold read access to the lake.

---

## 3. Lambda — `insightflow-crm-ingest`

Source: `lambdas/crm_ingest/lambda_function.py` (paste into the console editor —
self-contained, boto3 only, no layers or packaging needed)

- [ ] Runtime: Python 3.12
- [ ] Handler: `lambda_function.lambda_handler`
- [ ] Role: `insightflow-ingest-lambda-role`
- [ ] Timeout: 10s (an S3 PUT is fast; fail loudly rather than hang)
- [ ] Memory: 128 MB

Environment variables:

| Key | Value | Notes |
|---|---|---|
| `BRONZE_BUCKET` | `insightflow-bronze` | |
| `CRM_PREFIX` | `crm/events` | |
| `CLOSE_SIGNING_KEY` | *(unset)* | Unset ⇒ signature verification skipped, logs a WARNING each invocation. Set it when the SME issues the key; no code change needed. |

---

## 4. Lambda — `insightflow-calendly-ingest`

Source: `lambdas/calendly_ingest/lambda_function.py`

- [ ] Runtime: Python 3.12
- [ ] Handler: `lambda_function.lambda_handler`
- [ ] Role: `insightflow-ingest-lambda-role`
- [ ] Timeout: 10s
- [ ] Memory: 128 MB

| Key | Value | Notes |
|---|---|---|
| `BRONZE_BUCKET` | `insightflow-bronze` | |
| `CALENDLY_PREFIX` | `calendly/bookings` | `invitee.created` lands here |
| `CALENDLY_CANCEL_PREFIX` | `calendly/cancellations` | `invitee.canceled` lands here — separate prefix so a cancellation cannot overwrite its own creation object |
| `CALENDLY_SIGNING_KEY` | *(unset)* | Same behaviour as above. |

When the SMEs create the subscription, ask for **both** `invitee.created` and
`invitee.canceled` events. A cancellation we never receive cannot be
reconstructed, and cancelled bookings left in the counts inflate every booking
metric and flatter CPB.

---

## 5. API Gateway — REST API `insightflow-webhooks`

The sample payload in the requirement doc shows a REST API (it carries
`resource`, `httpMethod`, `requestContext.stage`), so we match that shape.

- [ ] Type: REST API (regional)
- [ ] Resource `/crm`, method **POST** → Lambda proxy → `insightflow-crm-ingest`
- [ ] Resource `/calendly`, method **POST** → Lambda proxy → `insightflow-calendly-ingest`
- [ ] **Lambda proxy integration must be ON** — the handlers read `event['body']`,
      `event['headers']` and `event['isBase64Encoded']`, which only exist in proxy mode
- [ ] Deploy to stage: `deploy`

Resulting URLs to hand to the SMEs:

```
POST https://{api-id}.execute-api.us-east-1.amazonaws.com/deploy/crm
POST https://{api-id}.execute-api.us-east-1.amazonaws.com/deploy/calendly
```

- [x] Real api-id: **`wx13s08t9k`**

---

## 6. Smoke test before handing the URLs over

```bash
curl -i -X POST https://{api-id}.execute-api.us-east-1.amazonaws.com/deploy/crm \
  -H 'Content-Type: application/json' \
  --data-binary @tests/fixtures/crm_event_created.json
```

Expect `200` with `{"message": "accepted", "key": "crm/events/dt=.../crm_event_ev_....json"}`,
and the object present in S3.

Idempotency check — send the **same** payload twice and confirm the bucket holds
**one** object, not two. That is D3 working: the key is a pure function of the
payload, so a retry overwrites itself byte-for-byte.

---

---

# Chunk 2 — CRM real-time path

Order matters: DLQ before the main queue (the main queue's redrive policy needs
the DLQ ARN), and the queue before the S3 notification.

## 7. DynamoDB tables

All three on **on-demand** billing — traffic is bursty and low, and provisioned
capacity would be guesswork.

| Table | Partition key | Purpose |
|---|---|---|
| `insightflow-event-ledger` | `event_id` (String) | Idempotency. One row per event, claimed atomically (D10). |
| `insightflow-lead-owner` | `lead_id` (String) | Read-through owner cache (D14). |
| `insightflow-awaiting-owner` | `lead_id` (String) | Durable worklist for leads whose owner has not landed (D15). |

- [ ] Created, all three

Optional: a TTL attribute on the ledger (say 90 days) keeps it from growing
forever. Do **not** put a TTL on the awaiting-owner table — expiry there would
silently abandon leads, which is precisely what the give-up rule exists to make
visible.

## 8. SQS — dead-letter queue

- [ ] Name: `insightflow-crm-dlq`, standard queue
- [ ] Message retention: 14 days (maximum — give yourself room to investigate)

## 9. SQS — delay queue

- [ ] Name: `insightflow-crm-delay`, **standard** (not FIFO — no ordering need,
      and FIFO's 5-minute dedup window is shorter than the 10-minute delay, so it
      would be structurally blind to the duplicates that matter)
- [ ] **Delivery delay: 10 minutes** — this is D6. Valid only because 10 < the
      15-minute cap; a longer delay would need Step Functions or EventBridge Scheduler
- [ ] Visibility timeout: **180 seconds**
- [ ] Redrive policy: DLQ `insightflow-crm-dlq`, **maxReceiveCount 5** (D13 —
      caps retries without losing the message)

⚠️ **Visibility timeout must exceed the enrichment Lambda's timeout**, or SQS
redelivers while the function is still running and you get duplicate work that
looks like a ledger bug. 30s function vs 180s visibility gives ample headroom.

Access policy — allow S3 to enqueue:

```json
{
  "Version": "2012-10-17",
  "Statement": [{
    "Effect": "Allow",
    "Principal": { "Service": "s3.amazonaws.com" },
    "Action": "sqs:SendMessage",
    "Resource": "arn:aws:sqs:us-east-1:{ACCOUNT_ID}:insightflow-crm-delay",
    "Condition": {
      "StringEquals": { "aws:SourceAccount": "{ACCOUNT_ID}" },
      "ArnLike": { "aws:SourceArn": "arn:aws:s3:::insightflow-bronze" }
    }
  }]
}
```

## 10. S3 event notification

On `insightflow-bronze`: Properties → Event notifications.

- [ ] Event type: `s3:ObjectCreated:*`
- [ ] **Prefix: `crm/events/`** — without it, every Calendly and Wistia object
      would also enqueue and the enrichment Lambda would fail on payloads it
      cannot parse
- [ ] Destination: SQS `insightflow-crm-delay`

The 10-minute delay is a property of the **queue**, not the notification — S3
cannot add a per-message delay, which is why the queue-level default is what we
set.

## 11. Lambda — `insightflow-crm-enrich`

Source: `lambdas/crm_enrich/lambda_function.py`

- [ ] Runtime: Python 3.12 · Handler: `lambda_function.lambda_handler`
- [ ] Timeout: **30s** (must stay ≤ the queue's visibility timeout)
- [ ] Memory: 256 MB
- [ ] Trigger: SQS `insightflow-crm-delay`, batch size 10,
      **Report batch item failures: ON** (the handler returns `batchItemFailures`;
      without this setting the whole batch is redelivered when one message fails)

| Env var | Value |
|---|---|
| `LEDGER_TABLE` | `insightflow-event-ledger` |
| `OWNER_CACHE_TABLE` | `insightflow-lead-owner` |
| `AWAITING_TABLE` | `insightflow-awaiting-owner` |
| `OWNER_BUCKET` | `dea-lead-owner` |
| `SLACK_WEBHOOK_URL` | *(your webhook)* — unset ⇒ alerts are logged, not posted |
| `LEASE_SECONDS` | `120` |

Role `insightflow-enrich-lambda-role`:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": "s3:GetObject",
      "Resource": "arn:aws:s3:::insightflow-bronze/crm/events/*"
    },
    {
      "Effect": "Allow",
      "Action": ["dynamodb:PutItem", "dynamodb:GetItem", "dynamodb:UpdateItem"],
      "Resource": [
        "arn:aws:dynamodb:us-east-1:{ACCOUNT_ID}:table/insightflow-event-ledger",
        "arn:aws:dynamodb:us-east-1:{ACCOUNT_ID}:table/insightflow-lead-owner",
        "arn:aws:dynamodb:us-east-1:{ACCOUNT_ID}:table/insightflow-awaiting-owner"
      ]
    },
    {
      "Effect": "Allow",
      "Action": ["sqs:ReceiveMessage", "sqs:DeleteMessage", "sqs:GetQueueAttributes"],
      "Resource": "arn:aws:sqs:us-east-1:{ACCOUNT_ID}:insightflow-crm-delay"
    }
  ]
}
```

Plus `AWSLambdaBasicExecutionRole`. Note there is **no** permission for the
public `dea-lead-owner` bucket — that read is unauthenticated by design, since
signing with our credentials against a bucket we do not own adds failure modes
for nothing.

## 12. Lambda — `insightflow-owner-sweep`

Source: `lambdas/owner_sweep/lambda_function.py`

- [ ] Runtime: Python 3.12 · Handler: `lambda_function.lambda_handler`
- [ ] Timeout: **60s** · Memory: 256 MB

| Env var | Value |
|---|---|
| `OWNER_CACHE_TABLE` | `insightflow-lead-owner` |
| `AWAITING_TABLE` | `insightflow-awaiting-owner` |
| `SLACK_WEBHOOK_URL` | *(your webhook)* |
| `MAX_RETRIES` | `24` (≈ one day of hourly sweeps before escalating) |

Role `insightflow-sweep-lambda-role`: `dynamodb:Scan`, `GetItem`, `PutItem`,
`UpdateItem`, `DeleteItem` on the owner-cache and awaiting-owner tables, plus
`AWSLambdaBasicExecutionRole`.

## 13. EventBridge Scheduler — hourly sweep

- [ ] Name: `insightflow-owner-sweep-hourly`
- [ ] Schedule: `rate(1 hour)`, flexible window off
- [ ] Target: `insightflow-owner-sweep`

A **scheduler**, not Step Functions: this is one periodic task, not a
multi-state workflow (D16). The cadence follows the downstream freshness need —
once the alert has fired the owner is analytics-only, so within-the-hour is fine.

## 14. Slack incoming webhook

- [ ] Create at <https://api.slack.com/messaging/webhooks>
- [ ] Set `SLACK_WEBHOOK_URL` on **both** Lambdas

Until it is set, both functions log the alert payload at WARNING instead of
posting. The path stays fully exercisable; nothing silently no-ops.

## 15. End-to-end verification

```bash
# One synthetic lead. Its owner file will 404, which is the interesting path.
python3 tools/replay.py --url https://{api-id}.execute-api.us-east-1.amazonaws.com/deploy/crm

# Idempotency: same event five times, concurrently.
python3 tools/replay.py --url ... --repeat 5 --concurrent
```

Expected after ~10 minutes:

| Check | Expectation |
|---|---|
| S3 | **One** object per `event_id`, regardless of send count |
| Slack | **One** alert per event — duplicates rejected by the conditional claim |
| Ledger | One row, `status = SENT` |
| Awaiting-owner | One row, `retry_count = 0`, for the synthetic lead |
| Owner cache | **No** row for that lead — a null owner is never negative-cached (D15) |

Then invoke `insightflow-owner-sweep` manually: the synthetic lead's
`retry_count` should increment to 1 and its status stay `AWAITING`.

---

---

# Chunk 3 — Calendly spend (scheduled pull)

No webhook registration needed. This one runs against real data immediately.

## 16. DynamoDB — spend manifest

- [ ] Table: `insightflow-spend-manifest`, partition key `asof_date` (String),
      on-demand

Records what we have landed and the content hash we computed for it. Two jobs:
it drives the self-healing diff, and it is the arbiter for S14 — a spend date
absent from Silver is only *genuine zero* if the manifest confirms we landed a
file covering it. Otherwise it is missing data and the metric is suppressed.

**No TTL.** Expiring manifest rows would make old dates look like ingestion
failures forever after.

## 17. Lambda — `insightflow-spend-ingest`

Source: `lambdas/spend_ingest/lambda_function.py`

- [ ] Runtime: Python 3.12 · Handler: `lambda_function.lambda_handler`
- [ ] Timeout: **120s** (30 sequential HTTPS GETs; measured well under this)
- [ ] Memory: 256 MB

| Env var | Value |
|---|---|
| `BRONZE_BUCKET` | `insightflow-bronze` |
| `SPEND_PREFIX` | `calendly/spend` |
| `MANIFEST_TABLE` | `insightflow-spend-manifest` |
| `SOURCE_BASE_URL` | `https://dea-data-bucket.s3.us-east-1.amazonaws.com/calendly_spend_data` |
| `VERIFY_ALL` | `true` — re-hash every advertised file each run. 30 small GETs a day, and the only way to notice an in-place correction. |

Role `insightflow-spend-lambda-role`: `s3:PutObject` on
`arn:aws:s3:::insightflow-bronze/calendly/spend/*`, plus `dynamodb:Scan` and
`PutItem` on the manifest table, plus `AWSLambdaBasicExecutionRole`.

No permission is needed for `dea-data-bucket` — it is read unauthenticated.

## 18. EventBridge Scheduler — daily pull

- [ ] Name: `insightflow-spend-daily`
- [ ] Schedule: `cron(30 6 * * ? *)`, **timezone `America/New_York`**
- [ ] Target: `insightflow-spend-ingest`

06:30 EST, a 30-minute buffer after the source's ~06:00 publication. Set the
timezone explicitly — a UTC cron would drift by an hour across DST and start
firing before the file exists.

Missing a run is not an outage: each file carries a 30-day trailing window, so
the next run recovers everything. See `SOURCE_CONTRACTS.md`.

## 19. Verification

```bash
# Manual invoke, then check the summary in CloudWatch.
aws lambda invoke --function-name insightflow-spend-ingest /dev/stdout

aws s3 ls s3://insightflow-bronze/calendly/spend/ --recursive | wc -l   # expect 30
```

Expected on a first run: `landed` lists 30 dates, `errors` empty,
`still_missing` empty. On a second run: `landed` empty, `unchanged` 30, and no
new S3 writes.

---

---

# Chunk 4 — Wistia (scheduled API pull)

No webhook registration needed; runs against real data immediately.

## 20. SSM Parameter Store — the API token

- [ ] Name: `/insightflow/wistia/api_token`
- [ ] Type: **SecureString**
- [ ] Value: the token from the requirement doc (page 6)

The repo is public and the token is marked "do not share". It must never be a
plaintext Lambda environment variable or reach a committed file. SecureString in
Parameter Store is free and sufficient; the handler reads it once per container
and caches it.

## 21. DynamoDB — Wistia manifest

- [ ] Table: `insightflow-wistia-manifest`, partition key `pull_key` (String),
      on-demand

Keys look like `events#8hunphufxp#2026-07-20`, `stats#{media}#{date}`,
`media#{media}#{asof}`. Wistia returns explicit zero rows for quiet days, but an
absent object is ambiguous — "no activity" or "our pull never ran". The manifest
settles it, the same role it plays for spend.

## 22. Lambda — `insightflow-wistia-ingest`

Source: `lambdas/wistia_ingest/lambda_function.py`

- [ ] Runtime: Python 3.12 · Handler: `lambda_function.lambda_handler`
- [ ] Timeout: **300s** (2 media × lookback days × paginated event calls;
      measured ~15s for a 3-day window, but the API has timed out once at 30s on
      a full page, so leave room for retries)
- [ ] Memory: 512 MB

| Env var | Value |
|---|---|
| `BRONZE_BUCKET` | `insightflow-bronze` |
| `MANIFEST_TABLE` | `insightflow-wistia-manifest` |
| `WISTIA_MEDIA_IDS` | `8hunphufxp,9k4tbcdfg0` |
| `WISTIA_TOKEN_PARAM` | `/insightflow/wistia/api_token` |
| `LOOKBACK_DAYS` | `7` |
| `PER_PAGE` | `50` (cap is 100; smaller is more reliable) |

⚠️ Do **not** set `WISTIA_API_TOKEN`. It exists only as a local-development
fallback and logs a warning when used.

Role `insightflow-wistia-lambda-role`:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": "s3:PutObject",
      "Resource": "arn:aws:s3:::insightflow-bronze/wistia/*"
    },
    {
      "Effect": "Allow",
      "Action": "dynamodb:PutItem",
      "Resource": "arn:aws:dynamodb:us-east-1:{ACCOUNT_ID}:table/insightflow-wistia-manifest"
    },
    {
      "Effect": "Allow",
      "Action": "ssm:GetParameter",
      "Resource": "arn:aws:ssm:us-east-1:{ACCOUNT_ID}:parameter/insightflow/wistia/api_token"
    },
    {
      "Effect": "Allow",
      "Action": "kms:Decrypt",
      "Resource": "arn:aws:kms:us-east-1:{ACCOUNT_ID}:alias/aws/ssm"
    }
  ]
}
```

Plus `AWSLambdaBasicExecutionRole`.

## 23. EventBridge Scheduler — daily pull

- [ ] Name: `insightflow-wistia-daily`
- [ ] Schedule: `cron(0 7 * * ? *)`, timezone `America/New_York`
- [ ] Target: `insightflow-wistia-ingest`

07:00 EST, after the spend pull. No coordination between them is needed — they
write to different prefixes.

A missed run is not an outage: the next run re-requests the whole lookback
window, and every day is independently re-requestable.

## 24. Backfill

Days are independent, so backfill needs no separate code path — invoke with an
explicit window:

```bash
aws lambda invoke --function-name insightflow-wistia-ingest \
  --payload '{"start_date":"2026-06-24","end_date":"2026-07-24"}' /dev/stdout
```

## 25. Verification

Expected object layout:

```
wistia/media/asof=YYYY-MM-DD/{hashed_id}.json          2 per day
wistia/media_stats/dt=YYYY-MM-DD/{hashed_id}.json      2 per day
wistia/visitor_events/dt=YYYY-MM-DD/{hashed_id}.json   2 per day
```

A 3-day window verified live: 14 objects, 12 events, 0 errors.

Watch the summary's `identification_rate`. It was **0.0** across every event
sampled during contract verification — the funnel's email bridge cannot populate
while that holds. See `SOURCE_CONTRACTS.md`, and report it as coverage rather
than as "video drove no bookings".

---

---

# Chunk 5 — Silver (Athena CTAS)

## 26. S3 buckets

- [ ] `insightflow-silver` — Parquet output, one prefix per table
- [ ] `insightflow-gold` — Parquet metric marts
- [ ] `insightflow-athena-results` — query results and metadata

Lifecycle rules worth setting: expire `insightflow-athena-results` after 30 days
(pure scratch), and expire `insightflow-silver/*/build_id=*` after 30 days so
pruned builds do not accumulate. Pruning drops the table definition only; the
data is left in place deliberately, so a mistaken prune is recoverable.

## 27. Athena workgroup

- [ ] Name: `insightflow`
- [ ] Query result location: `s3://insightflow-athena-results/`
- [ ] Engine version: **Athena engine v3**

## 28. Create the databases and Bronze tables

Run once, in order, in the Athena console:

```
sql/bronze/00_create_database.sql     3 databases
sql/bronze/10_external_tables.sql     11 external tables
```

Partitions are projected from the key layout, so there is no crawler to run and
no `MSCK REPAIR TABLE` after a load.

## 29. Publish the SQL and seeds to S3

The Lambda reads its templates from S3 so it stays a single console-deployable
file. The repo remains the source of truth:

```bash
aws s3 sync sql/ s3://insightflow-bronze/sql/ --delete
aws s3 cp seeds/channel_map.ndjson \
  s3://insightflow-bronze/seeds/channel_map/channel_map.ndjson
aws s3 cp seeds/custom_field_map.ndjson \
  s3://insightflow-bronze/seeds/custom_field_map/custom_field_map.ndjson
```

⚠️ Re-run the sync after editing any `.sql` file, or the build runs the old
version. This is the main cost of console-first and the first thing CI should
automate.

## 30. Lambda — `insightflow-owner-export`

Source: `lambdas/owner_export/lambda_function.py`

- [ ] Runtime: Python 3.12 · Timeout 120s · Memory 256 MB
- [ ] Env: `BRONZE_BUCKET`, `OWNER_CACHE_TABLE`, `AWAITING_TABLE`

Role: `dynamodb:Scan` on the owner-cache and awaiting tables, `s3:PutObject` on
`insightflow-bronze/crm/*`.

Athena cannot read DynamoDB, so this lands the owner state as NDJSON before each
build. Snapshotted per `asof=` rather than overwritten, because the coverage
table's value is its trajectory.

## 31. Lambda — `insightflow-warehouse-build`

Source: `lambdas/warehouse_build/lambda_function.py`

- [ ] Runtime: Python 3.12 · Timeout **900s** · Memory 512 MB

| Env var | Value |
|---|---|
| `SQL_BUCKET` | `insightflow-bronze` |
| `SILVER_BUCKET` / `GOLD_BUCKET` | `insightflow-silver` / `insightflow-gold` |
| `ATHENA_WORKGROUP` | `insightflow` |
| `ATHENA_OUTPUT` | `s3://insightflow-athena-results/silver/` |
| `OWNER_EXPORT_FUNCTION` | `insightflow-owner-export` |
| `KEEP_BUILDS` | `3` |

Role needs: `athena:StartQueryExecution` / `GetQueryExecution` /
`GetQueryResults`, `glue:*Table*` and `glue:*Database*` on the three databases,
`s3` read on `insightflow-bronze`, read/write on `insightflow-silver` and
`insightflow-athena-results`, and `lambda:InvokeFunction` on
`insightflow-owner-export`.

## 32. EventBridge Scheduler — daily build

- [ ] Name: `insightflow-warehouse-daily`
- [ ] Schedule: `cron(30 7 * * ? *)`, timezone `America/New_York`
- [ ] Target: `insightflow-warehouse-build`

07:30 EST, after the spend (06:30) and Wistia (07:00) pulls. The owner export is
**not** scheduled separately — the build invokes it synchronously, so ordering
is a guarantee rather than a gap between two crons.

## 33. Verification

```bash
aws lambda invoke --function-name insightflow-warehouse-build /dev/stdout
```

Expect `status: SUCCEEDED`, ten Silver tables and eight Gold marts in `built`,
the same in `views_swapped`. Then in Athena:

```sql
SELECT * FROM insightflow_silver.dq_lead_coverage ORDER BY build_date DESC;

-- The check that matters most. If these disagree, the 30-file overlap is not
-- being collapsed and CPB is inflated.
SELECT COUNT(*) AS rows, COUNT(DISTINCT (spend_date, channel)) AS keys
FROM insightflow_silver.fct_spend;
```

⚠️ **The SQL has never been executed.** Athena cannot run locally, so the
templates are validated structurally, not semantically. Expect to fix syntax on
the first run — most likely candidates are the `AT TIME ZONE` expressions, the
`$path` reference in `fct_media_daily_stats`, and the `UNNEST` cast in
`fct_booking_host`.

## 34. Gold marts

Built by the same Lambda in the same invocation, after Silver's views are
swapped — Gold reads those views, so splitting the layers across two schedules
would reintroduce a timing race.

| Mart | Grain | Spec metric |
|---|---|---|
| `daily_calls_by_source` | booking_date + channel | 1.1 |
| `cpb_by_channel` | metric_date + channel | 1.2 |
| `bookings_trend` | booking_date + channel | 1.3 |
| `channel_attribution` | channel | 1.4 |
| `booking_time_slots` | hour + dow + channel + perspective | 1.5 |
| `meeting_load_by_employee` | host | 1.6 |
| `media_engagement` | hashed_id + stat_date | Wistia |
| `video_booking_funnel` | channel + booking_date | cross-source |

Checks worth running once it builds:

```sql
-- Spend must not have fanned out. If total_spend is ~30x a hand-check of one
-- day's file, the Silver dedup is not working.
SELECT channel, total_spend, total_bookings, cpb, spend_coverage_rate
FROM insightflow_gold.channel_attribution ORDER BY rank_by_efficiency;

-- Expect video_touch_rate_floor = 0 WITH video_identification_rate = 0.
-- That pairing means "not measurable", not "video drove nothing".
SELECT channel, bookings, bookings_with_prior_video,
       video_touch_rate_floor, video_identification_rate, interpretation
FROM insightflow_gold.video_booking_funnel ORDER BY booking_date DESC LIMIT 10;

-- Per-employee load will NOT sum to total meetings. Correct: a co-hosted
-- meeting is one meeting and two units of load.
SELECT SUM(total_meetings) FROM insightflow_gold.meeting_load_by_employee;
SELECT COUNT(*) FROM insightflow_silver.fct_booking;
```

## Not yet built (later chunks)
- Athena workgroup + Glue database + results bucket — chunk 5
- Silver/Gold CTAS builds + Step Functions orchestration — chunks 5–6
- Streamlit hosting (ECS Fargate or App Runner — the AWS-only constraint rules
  out Streamlit Community Cloud) — chunk 7
