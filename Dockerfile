FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg fontconfig fonts-dejavu-core libgl1 libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
RUN useradd -m app && mkdir -p /app/data && chown -R app /app/data
USER app
ENV HOST=0.0.0.0 PORT=8000 DATA_DIR=/app/data
VOLUME /app/data
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s CMD python -c "import urllib.request,os;urllib.request.urlopen('http://127.0.0.1:%s/api/health'%os.environ.get('PORT','8000'))"
CMD ["python", "run.py", "--no-browser"]
