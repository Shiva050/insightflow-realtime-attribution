# Verified source contracts

What each source **actually** returns, checked against the live endpoint rather
than inferred from the requirement doc or a happy-path sample.

This file exists because one such check already changed the design. Endpoint
shape, sort order, cap values and nullability are things to read and verify, not
assume — and where a verified contract contradicts the spec, that contradiction
is recorded here rather than silently coded around.

---

## Calendly spend — `dea-data-bucket`

Verified **2026-07-24** against the live public bucket.

Base: `https://dea-data-bucket.s3.us-east-1.amazonaws.com/calendly_spend_data/`

### `file_index.json`

```json
{ "files": ["spend_data_2026-06-24.json", "…", "spend_data_2026-07-23.json"] }
```

- A **rolling 30-day view**, newest last.
- ⚠️ It is **not a complete listing**. `spend_data_2026-06-23.json` returns 200
  despite being absent from the index. Files age out of the index but remain
  fetchable, so backfill beyond 30 days is possible by constructing filenames.
- Today's file does not exist (403). The latest available is always **Day-1**,
  matching the spec's "06:00 EST daily for Day-1 spends".

### `spend_data_YYYY-MM-DD.json`

```json
[ { "date": "2026-06-24", "channel": "facebook_paid_ads", "spend": 622.0 } ]
```

- Flat array. Keys: `date`, `channel`, `spend` (USD, float).
- Three channels: `facebook_paid_ads`, `youtube_paid_ads`, `tiktok_paid_ads`.

### ⚠️ The filename date is an AS-OF date, not the data's date

**This contradicts the natural reading of the spec.** "Daily for Day-1 spends"
suggests one day of data per file. It is not.

Each file carries a **30-day trailing window**:

| File | Rows | Distinct dates | Covers |
|---|---|---|---|
| `spend_data_2026-07-21.json` | 90 | 30 | `2026-06-22 .. 2026-07-21` |
| `spend_data_2026-07-22.json` | 90 | 30 | `2026-06-23 .. 2026-07-22` |
| `spend_data_2026-07-23.json` | 90 | 30 | `2026-06-24 .. 2026-07-23` |

90 rows = 30 dates × 3 channels. Across the 30 advertised files, 59 distinct
spend dates are covered, and `2026-06-24` appears in **all 30**.

Consecutive files were compared: 87 shared `(date, channel)` keys, **0
disagreements**. Corrections are therefore possible but were not observed.

### Consequences

1. **Bronze partitions on `asof=`, not `dt=`.** Every other source uses `dt=`
   for the event's own date. Here the filename date is a publication date
   covering 30 other dates, so `dt=` would invite a later join on the wrong
   column and be quietly wrong.

2. **Silver must deduplicate.** The same `(spend_date, channel)` appears in up
   to 30 files. Resolution is last-write-wins ordered by **as-of date** — the
   newest file wins, because corrections propagate forward. Summing Bronze rows
   without this collapse inflates spend up to 30×, which is the S12 fan-out
   trap arriving one layer earlier than expected.

3. **Self-healing is stronger than C7 assumed.** A missed day loses nothing:
   the next file re-covers 29 of the same 30 dates. Only **30 consecutive**
   missed days lose a spend date permanently.

4. **The content hash (C8) still earns its place**, but for a narrower job:
   catching an as-of file revised in place. Corrections to historical dates
   arrive free in the next window. We compute the hash ourselves and never
   trust the source ETag (not a plain MD5 for multipart uploads, and it belongs
   to a bucket we do not own) or `last_modified` (moves on touch-without-change).

5. **The manifest remains the arbiter for S14.** "Bookings but no spend row" is
   only *organic* if our manifest confirms we landed a file covering that date.
   Otherwise it is missing data and the metric must be suppressed, not reported
   as a flatteringly infinite efficiency.

---

## Close CRM — webhook

Not yet verified against live traffic: the subscription is created by the SMEs
and had not been registered at the time of writing. Shape below is from the
requirement doc's sample payload.

- API Gateway **REST** proxy event (carries `resource`, `httpMethod`,
  `requestContext.stage`), body is a JSON string.
- Body: `{"subscription_id": ..., "event": {...}}`
- `event.id` is the event identity; `event.lead_id` the lead; `event.action` is
  `created` / `updated`; `event.changed_fields` is `[]` on creation.
- Lead fields live under `event.data`, including opaque `custom.cf_*` keys.
- Signature headers: `Close-Sig-Hash`, `Close-Sig-Timestamp`.

**To verify once live:** the exact HMAC signing scheme, whether `date_created`
ever carries a timezone offset (the sample has none at `event` level but does at
`event.data` level), and whether `action` takes values beyond created/updated.

## Lead owner lookup — `dea-lead-owner`

- `https://dea-lead-owner.s3.us-east-1.amazonaws.com/{lead_id}.json`
- Fields: `lead_id`, `display_name`, `lead_email`, `lead_owner`, `funnel`,
  `status_label`, `date_created`.
- A missing owner returns **403**, not 404, because bucket listing is denied.
  Both are treated as "not yet assigned".

## Calendly webhook

Not yet verified against live traffic. Shape from the requirement doc's sample.

- Body: `{"created_at":…, "event": "invitee.created", "payload": {…}}`
- `payload.uri` is the invitee URI, a child path of `payload.scheduled_event.uri`.
- `payload.scheduled_event.event_memberships` is an **array** — meetings can
  have multiple hosts.
- `payload.tracking.utm_*` were **all null** in the sample. The spec asks for
  channel attribution "using UTM parameters"; if UTMs are null in practice, the
  `event_type` → channel map is the only workable route.

**To verify once live:** whether UTM fields are ever populated, and whether
`invitee.canceled` payloads carry a nested `cancellation` object.

## Wistia — Stats API

Not yet verified. To check **before** designing against it (this is the check
that flipped the whole Wistia design once already):

- Does `/stats/medias/{id}/by_date` exist and accept a date range?
- Sort order of paginated results — ascending or descending? A newest-first API
  makes a mid-run watermark advance silently lose older records.
- `per_page` cap (documented as 100).
- Whether the events endpoint exposes `visitor_key`, `email` and `media_id` on
  one row.
