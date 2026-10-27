# Sleuth case service: FastAPI web + in-process worker (python -m service).
# Read-only investigator; holds no keys. State lives in $SLEUTH_HOME (a mounted volume in production).
FROM python:3.13-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY sleuth ./sleuth
COPY service ./service
ENV SLEUTH_HOST=0.0.0.0 SLEUTH_PORT=8080 SLEUTH_HOME=/data SLEUTH_TRUST_PROXY=1
EXPOSE 8080
CMD ["python", "-m", "service"]
