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

## Not yet built (later chunks)

- SQS delay queue (10 min) + S3 event notification — chunk 2
- DynamoDB idempotency ledger, owner cache, awaiting-owner worklist — chunk 2
- Slack incoming webhook — chunk 2
- DLQ + reconciliation sweep (EventBridge Scheduler) — chunk 2
- Athena workgroup + Glue database + results bucket — chunk 5
- Streamlit hosting (ECS Fargate or App Runner — AWS-only constraint rules out
  Streamlit Community Cloud) — chunk 7
