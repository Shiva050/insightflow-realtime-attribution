FROM python:3.12-slim

WORKDIR /app
COPY streamlit/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY streamlit/ /app/streamlit/

ENV AWS_REGION=us-east-1 \
    ATHENA_WORKGROUP=insightflow \
    ATHENA_OUTPUT=s3://insightflow-athena-results/dashboard/ \
    STREAMLIT_SERVER_PORT=8501 \
    STREAMLIT_SERVER_ADDRESS=0.0.0.0 \
    STREAMLIT_SERVER_HEADLESS=true \
    STREAMLIT_BROWSER_GATHER_USAGE_STATS=false

EXPOSE 8501

# Credentials come from the ECS task role at runtime, never baked into the
# image. An image carrying keys is a credential leak that survives every
# `docker push`.
#
# python:3.12-slim ships no curl, so the check uses the interpreter that is
# already here. A healthcheck calling a missing binary reports unhealthy
# forever, and ECS would kill and restart the task in a loop.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import urllib.request,sys; \
sys.exit(0 if urllib.request.urlopen('http://localhost:8501/_stcore/health', timeout=4).status==200 else 1)"

CMD ["streamlit", "run", "streamlit/app.py"]
