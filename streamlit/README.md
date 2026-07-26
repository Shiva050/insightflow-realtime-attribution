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

Not currently hosted — the dashboard runs locally, which covers the deliverable
(reports/dashboards plus a demo recording). `Dockerfile` documents the
production path rather than being part of the build; nothing in CI builds it.

If it ever needs a URL:

```
Dockerfile  --docker build-->  image  --docker push-->  ECR  --pull & run-->  ECS Fargate  --> ALB --> https URL
```

**ECS Fargate behind an ALB**, not App Runner or Lambda. Streamlit holds a
persistent WebSocket to push reruns: Lambda cannot do that at all, and App
Runner's WebSocket support must be verified before committing to it — an ALB
supports them natively. EKS would be Kubernetes overhead for one stateless
container.

The app would authenticate through the **ECS task role**, so no credentials ever
enter the image.

Two things to settle first: an ALB URL is unauthenticated by default and this
dashboard shows spend, CPB and per-employee workload, so it needs Cognito on the
listener or a VPC-internal placement; and the ALB costs about $16/month whether
anyone visits or not.

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
