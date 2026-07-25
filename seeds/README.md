# Seeds — externalised reference maps

These are the static reference mappings the transforms depend on. They are
**config, not code** (C4, S4): onboarding a new campaign or reacting to a Close
field reconfiguration should be an S3 object update, not a code change and
redeploy.

## Where the real values live

The files in this directory are **examples with placeholder IDs**. The real maps
live in S3 and are read at transform time:

```
s3://insightflow-bronze/seeds/channel_map.json
s3://insightflow-bronze/seeds/custom_field_map.json
```

Two reasons they are not committed with real values:

1. This repo is public, and the real IDs come from the requirement docs.
2. C4 calls for the map to be externalised anyway — a seed in S3 *is* the
   design, not a workaround for the first reason.

## channel_map.json

Maps a Calendly `event_type` URI to a marketing channel label. Applied as a
`channel` column in Silver; each Gold metric then decides which channels it
wants (C5 — Silver tags, Gold selects).

Bookings whose `event_type` is absent from this map are **not dropped**. They
get `channel = 'other'`, because the webhook subscription is org-wide and
`Daily Calls Booked by Source` needs every source, not just the paid three.

## custom_field_map.json

Maps Close's opaque `custom.cf_*` field IDs to readable names — most importantly
the one carrying `funnel`. These IDs are vendor-generated and meaningless on
sight; hardcoding them in transform SQL means a silent break if Close is ever
reconfigured.

Note the field is sourced from the **event payload**, not the owner-lookup file
(S3 — field-level source of truth). Every lead has an event; not every lead has
an owner file, so sourcing `funnel` from the lookup would null it out for
exactly the owner-less leads the awaiting-owner worklist exists to track.
