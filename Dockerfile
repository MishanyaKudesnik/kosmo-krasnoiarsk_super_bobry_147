FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=/app/src
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
# The case dataset (~44 MB) is pulled from the organisers' public Yandex Disk and
# checksum-verified at build time, which keeps the upload small.
# If data/ was shipped with the source, it is used as is.
RUN [ -f data/file_catalog.csv ] || python tools/fetch_data.py
# Railway (and most PaaS) pass the listening port in $PORT.
CMD ["sh", "-c", "python -m canopy.cli serve --host 0.0.0.0 --port ${PORT:-8000}"]
