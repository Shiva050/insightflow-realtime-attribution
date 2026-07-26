# InsightFlow dashboard

Streamlit over Athena. Reads Gold marts only — there is deliberately no metric
logic here, so a number on screen always traces back to a CTAS and two consumers
of the same mart cannot disagree.

## Run locally

```bash
pip install -r streamlit/requirements.txt
export AWS_REGION=us-east-1
export ATHENA_WORKGROUP=insightflow
export ATHENA_OUTPUT=s3://insightflow-athena-results/dashboard/
streamlit run streamlit/app.py
```

Needs AWS credentials with `athena:StartQueryExecution`, `GetQueryExecution`,
`GetQueryResults`, Glue read on the databases, S3 read on `insightflow-silver`
and `insightflow-gold`, and read/write on the results bucket.

Every panel degrades to an explanatory message if its table is missing, so the
app is runnable before the warehouse has ever built.

## Hosting

Streamlit Community Cloud is **not** an option — the spec restricts the stack to
AWS/Azure. Deploy to ECS Fargate or App Runner behind the container in
`streamlit/Dockerfile`.

## Chart conventions

The palette is validated for colour-vision deficiency in both modes (worst
adjacent CVD ΔE 9.1 light / 8.4 dark). Two light-mode hues fall below 3:1
contrast against the surface, so the relief rule applies: every chart carries a
legend **and** a table view, and no chart encodes meaning by colour alone.

Channel colour follows the entity, never its rank — filtering to two channels
does not repaint them.

Three display rules exist to stop the dashboard lying:

- **CPB is omitted, not zeroed,** when bookings are zero. Undefined is not free.
- **Days with missing spend are excluded** from totals and flagged, rather than
  counted as zero — a failed pull must not make a channel look efficient.
- **The funnel says "not measurable"** when the video identification rate is
  zero, instead of rendering a 0% touch rate. "Cannot measure" and "measured
  zero" are different claims.
