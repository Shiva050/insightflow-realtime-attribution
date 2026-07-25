# InsightFlow

[![tests](https://github.com/Shiva050/insightflow-realtime-attribution/actions/workflows/tests.yml/badge.svg?branch=dev)](https://github.com/Shiva050/insightflow-realtime-attribution/actions/workflows/tests.yml)

Real-time attribution across CRM, meeting-scheduling and video-engagement data.
Close, Calendly and Wistia land in a medallion lakehouse on AWS, feeding both a
sales alerting path and a cross-source marketing funnel.

## Architecture

| Layer | Where | How |
|---|---|---|
| **Bronze** | S3, raw JSON | Natural-key filenames, immutable, replayable |
| **Transform** | EventBridge → Lambda → Athena CTAS | Full rebuild each run |
| **Silver / Gold** | S3 Parquet, Glue Data Catalog | Partitioned by date (+ channel) |
| **Serve** | Streamlit → Athena | |

No dbt, no Snowflake, AWS only — all spec constraints.

### Feeds

| Source | Arrival | Bronze grain |
|---|---|---|
| Close CRM | webhook, real-time | `event_id` |
| Calendly bookings | webhook, real-time | `invitee_uri` |
| Calendly spend | scheduled pull, self-healing | as-of date |
| Wistia | scheduled API pull, paginated | `hashed_id + date`, `event_key` |

### CRM real-time path

```
webhook → API GW → Lambda → S3 → SQS (10-min delay) → Lambda → Slack
                                        ↓
                          DynamoDB ledger (atomic claim)
                          owner cache (read-through)
                          awaiting-owner worklist → hourly sweep
```

The delay gives Close time to assign a lead owner. Idempotency is an atomic
conditional write, not read-then-write, which would be a TOCTOU race. Alerts
send before the ledger records them: exactly-once is unavailable, and a
duplicate page beats a missed lead.

## Layout

```
lambdas/          one self-contained handler per directory (console-deployable)
sql/              Athena CTAS builds for Silver and Gold
seeds/            externalised reference maps (real values live in S3)
infra/            console setup record — what was clicked, and why
tools/            replay harness and test runner
tests/            113 checks, no AWS required
SOURCE_CONTRACTS.md   verified source behaviour, incl. where it contradicts the spec
```

## Running the tests

```bash
python3 tools/run_tests.py        # everything
python3 tests/test_enrich.py      # one suite
```

No AWS credentials, no network, about a second. CI runs the same entry point on
every push and pull request against `main` and `dev`, on both Python 3.9 and
3.12 (the Lambda runtime).

A second CI job refuses any commit containing a credential or the requirement
documents — this repo is public and the requirement PDF carries a live API
token, so `docs/` is gitignored and its absence is enforced rather than trusted.

## Exercising the pipeline

The Close and Calendly webhook subscriptions are registered by the SMEs. Until
that lands, the replay harness drives the full path with synthetic events:

```bash
python3 tools/replay.py --dry-run
python3 tools/replay.py --url https://{api-id}.execute-api.us-east-1.amazonaws.com/deploy/crm
python3 tools/replay.py --url ... --repeat 5 --concurrent    # idempotency
```

## Deployment

Infrastructure is built console-first; `infra/console-setup.md` is the record of
every resource, its settings, and the reasoning — kept current so it can be
codified later by transcription rather than archaeology.
