# Seeds — externalised reference maps

The static reference mappings the transforms depend on. They are **config, not
code** (C4, S4): onboarding a new campaign or reacting to a Close field
reconfiguration should be an S3 object update, not a code change and redeploy.

## Where the real values live

The files here are **examples with placeholder IDs**. The real maps live in S3
and are read at transform time. Note the paths are **prefixes containing a
file**, not files — Athena's `LOCATION` points at a directory:

```
s3://insightflow-bronze/seeds/channel_map/channel_map.ndjson
s3://insightflow-bronze/seeds/custom_field_map/custom_field_map.ndjson
```

Two reasons the real values are not committed:

1. This repo is public, and the real IDs come from the requirement docs.
2. C4 calls for the map to be externalised anyway — a seed in S3 *is* the
   design, not a workaround for the first reason.

## Format: NDJSON, one mapping per line

Same framing as the rest of Bronze, for the same reason: Athena's JSON SerDe is
line-oriented. One row per mapping also means a seed joins like any other table,
rather than needing a nested object unpacked in SQL.

## channel_map

Maps a Calendly `event_type` URI to a marketing channel label. Applied as a
`channel` column in Silver; each Gold metric then decides which channels it
wants (C5 — Silver tags, Gold selects).

```json
{"event_type":"https://api.calendly.com/event_types/...","channel":"facebook_paid_ads","is_paid":true}
```

Bookings whose `event_type` is absent from this map are **not dropped**. They
get `channel = 'other'`, because the webhook subscription is org-wide and
`Daily Calls Booked by Source` needs every source, not just the paid three.

`is_paid` exists so CPB can filter to channels where spend exists without
hardcoding the three names into a metric query.

## custom_field_map

Maps Close's opaque `custom.cf_*` field IDs to readable names — most importantly
the one carrying `funnel`. These IDs are vendor-generated and meaningless on
sight; hardcoding them in transform SQL means a silent break if Close is ever
reconfigured.

```json
{"field_id":"custom.cf_...","field_name":"funnel"}
```

Note `funnel` is sourced from the **event payload** for `dim_lead`, not from the
owner-lookup file (S3 — field-level source of truth). Every lead has an event;
not every lead has an owner file, so sourcing it from the lookup would null it
out for exactly the owner-less leads the awaiting-owner worklist exists to
track.

The **notification** payload is the exception and takes `funnel` from the
lookup, because that is what the spec asks for.

## Uploading

```bash
aws s3 cp channel_map.ndjson \
  s3://insightflow-bronze/seeds/channel_map/channel_map.ndjson
aws s3 cp custom_field_map.ndjson \
  s3://insightflow-bronze/seeds/custom_field_map/custom_field_map.ndjson
```

No pipeline redeploy is needed. Silver is rebuilt from Bronze plus current seed
state on every run, so a corrected mapping lands on the next build.
