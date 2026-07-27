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
### Signing scheme — verified 2026-07-26 against developer.close.com

- Headers: `Close-Sig-Hash` (signature), `Close-Sig-Timestamp` (signing time).
- Signed string is `close-sig-timestamp + body`, concatenated with **no
  separator**.
- HMAC-SHA256, hex digest, compared constant-time.
- **The `signature_key` is a hex string and must be hex-DECODED before use as
  the HMAC key** (`bytes.fromhex(key)`). This is the trap: passing the hex text
  straight to `hmac.new()` turns a 64-character key into 64 ASCII bytes instead
  of the intended 32, so every signature mismatches and every genuine webhook
  gets a 401. The key is issued in the POST response when the subscription is
  created.

Note the asymmetry with Calendly below — different separator, different key
encoding. The two verifiers are deliberately kept separate for that reason.

**To verify once live:** whether `date_created` ever carries a timezone offset
(the sample has none at `event` level but does at `event.data` level), and
whether `action` takes values beyond created/updated.

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

### Signing scheme — 2026-07-26

- Header: `Calendly-Webhook-Signature`, value `t=<unix_ts>,v1=<hex_signature>`.
- Signed string is `t + "." + body` — note the **dot separator**, unlike Close.
- HMAC-SHA256, hex digest. The signing key is used as **raw UTF-8**, NOT
  hex-decoded — again unlike Close.
- The `signing_key` is chosen by us and supplied when the subscription is
  created, so it can be generated and stored before the SMEs register anything.

Confirmed from Calendly's published verification example. Calendly's docs site
renders client-side and could not be fetched directly, so unlike the Close entry
this one is **not** first-party-verified — treat it as provisional and confirm
against the first live delivery.

**To verify once live:** the signature scheme above, whether UTM fields are ever
populated, and whether `invitee.canceled` payloads carry a nested `cancellation`
object.

## Wistia — Stats API

Verified **2026-07-24** against the live API. Base `https://api.wistia.com/v1/`.

**Auth is `Authorization: Bearer {token}`.** Basic auth with `api:{token}` —
the older documented scheme — returns 401.

### `/stats/medias/{hashed_id}/by_date.json` — exists, and is a flow

```json
{ "date": "2026-07-24", "load_count": 565, "play_count": 5, "hours_watched": 0.209 }
```

- One row per date. `play_count` is already that day's activity, so no
  snapshot-differencing, no `LAG`, no seed-row trap, no negative clamping. The
  entire W3/W4 stock apparatus is unnecessary.
- **`start_date` / `end_date` are honoured and INCLUSIVE.** A range request
  returned exactly `2026-07-01..2026-07-10`. Missed days are therefore
  independently re-requestable — recovery is self-healing, not prevention-only.
- Zero-activity days are returned as rows of zeros, so the series has no gaps.
- **`play_rate` is NOT in the payload** — only `load_count` and `play_count`.
  Carry both to the target grain and divide once (W7).

The cumulative endpoint `/stats/medias/{id}.json` does exist and does expose
`play_rate`, `visitors` and `engagement` — but it is a **stock**, lifetime to
date. We design against `by_date`.

Why W7 matters here, from real numbers over `2026-06-24..2026-07-24`:

| media | loads | plays | play_rate |
|---|---|---|---|
| `8hunphufxp` | 6455 | 48 | **0.74%** |
| `9k4tbcdfg0` | 44 | 18 | **40.91%** |

Averaging the two rates gives ~21%. The correct volume-weighted figure is
66/6499 = **1.02%**. A 20× error, from exactly the non-additive-ratio mistake
W7 describes.

### `/stats/events.json` — the visitor bridge

Row carries `event_key`, `visitor_key`, `media_id`, `received_at`,
`percent_viewed`, `email`, `ip`, `country`/`region`/`city`, `org`,
`user_agent_details`, `conversion_type`.

So one row holds both the visitor↔media link and the email the funnel joins on.

- **`media_id`, `start_date`, `end_date` filters all work**, and date bounds are
  inclusive. The feed can be pulled as bounded day-windows.
- Unfiltered, the endpoint returns **org-wide** events across all media in the
  account, not just the two in scope.

### ⚠️ Pagination is NEWEST-FIRST (descending)

Verified directly: page 1 ran `05:24:19 → 03:06:59`, page 2 continued
`02:01:39 → 01:13:36`. Pages are disjoint.

**This is the W5 hazard, confirmed rather than hypothesised.** Advancing a
watermark to the newest record seen mid-run would set it to the global maximum,
and every unfetched older record becomes permanently invisible — no error, no
gap, just silently absent data. The watermark must be a commit marker advanced
only after every page lands.

Better still, and what we do: because the endpoint accepts inclusive date
bounds, ingest **day-windowed** rather than cursor-driven. Each day is
independently re-requestable, there is no cursor state to corrupt, and sort
order stops mattering inside a bounded window.

### `per_page` cap is 100

Requesting 200 or 500 both return 100. A `per_page=100` request also **timed out
once** at 30s during verification, so the client needs a retry and should prefer
smaller pages.

### `/stats/visitors.json`

Keys: `visitor_key`, `created_at`, `last_active_at`, `load_count`, `play_count`,
`visitor_identity`, `identifying_event_key`, `last_event_key`,
`user_agent_details`.

`visitor_identity` is nested: `{"name": "", "email": null, "org": {...}}`.

Note this row mixes stable identity with counts that move on every watch —
which is precisely why S17 splits it into `dim_visitor` plus a separate fact.

### ⚠️ The identification rate is approximately ZERO

Of 100 sampled events, **0 carried an email**. Of 25 sampled visitors, **0** had
`visitor_identity.email` populated.

S22 anticipated that anonymous viewing is normal and should be reported rather
than cleaned away. In this account it is not merely common, it is total.

Consequences for the cross-source funnel:

- The email bridge `fct_visitor_event → dim_lead` is structurally correct but
  currently joins **nothing**. The key exists; it is empty.
- F2's "video-touch rate is a floor, not an exact rate" still holds — but the
  floor here is 0, and a floor of zero carries no information.
- This must be reported as a coverage figure alongside the funnel, never as a
  finding that video drives no bookings. Those are entirely different claims,
  and conflating them would be the confident-wrong-number failure S7 describes.
- Media-quality metrics (plays, loads, play_rate per video per day) are
  unaffected and fully computable.

**To re-check before the 7-day run:** whether any Wistia embed captures email
(a Turnstile form or a post-roll email gate). If none does, the funnel's video
leg cannot populate, and that should be stated in the SME presentation as a
source limitation rather than discovered in the dashboard.
