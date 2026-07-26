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

# Credentials come from the task role, never baked into the image.
HEALTHCHECK CMD curl --fail http://localhost:8501/_stcore/health || exit 1

CMD ["streamlit", "run", "streamlit/app.py"]
