FROM python:3.13-slim-bookworm

# System deps for Shapely (libgeos)
RUN apt-get update && \
    apt-get install -y --no-install-recommends gcc libgeos-dev && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python deps
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY optimizer/ /app/optimizer/
COPY scraper/ /app/scraper/
COPY webapp/ /app/webapp/

# Create data directories
RUN mkdir -p /app/webapp/cache /app/webapp/maps

# Set working directory to webapp for Flask
WORKDIR /app/webapp

ENV PYTHONUNBUFFERED=1
ENV FLASK_APP=app.py
ENV CACHE_DIR=/app/webapp/cache
ENV MAPS_DIR=/app/webapp/maps

EXPOSE 5000

CMD ["gunicorn", "--bind", "0.0.0.0:5000", "--workers", "2", "--threads", "4", "--timeout", "300", "app:app"]
