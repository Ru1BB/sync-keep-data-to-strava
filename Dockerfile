FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=8080 \
    DATA_DIR=/app/data

WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY app.py ./
COPY templates ./templates
COPY static ./static
RUN useradd --system --uid 10001 --create-home appuser && mkdir -p /app/data && chown -R appuser:appuser /app
USER appuser
EXPOSE 8080
CMD ["gunicorn", "--workers", "1", "--threads", "8", "--timeout", "180", "--bind", "0.0.0.0:8080", "app:app"]
