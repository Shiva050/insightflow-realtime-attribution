# Infrastructure — console setup record

We are building console-first. This file is the record of what was clicked, so
the infrastructure can be codified later by transcription rather than
archaeology. **Update it as you click.** An undocumented console resource is a
resource nobody can rebuild.

Region for everything: **us-east-1** (the public source buckets — `dea-lead-owner`
and `dea-data-bucket` — are us-east-1; staying local avoids cross-region
transfer and latency).

Status legend: `[ ]` not created · `[x]` created and verified

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

- [ ] Record the real `{api-id}` here once deployed: `__________`

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

## Not yet built (later chunks)

- Calendly spend batch puller, self-healing via `file_index.json` — chunk 3
- Wistia API puller, pagination + watermark — chunk 4
- Athena workgroup + Glue database + results bucket — chunk 5
- Silver/Gold CTAS builds + Step Functions orchestration — chunks 5–6
- Streamlit hosting (ECS Fargate or App Runner — the AWS-only constraint rules
  out Streamlit Community Cloud) — chunk 7
