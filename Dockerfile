FROM python:3.11-slim

ENV TZ=Asia/Shanghai \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY app/ app/
RUN mkdir -p /data && ln -snf /usr/share/zoneinfo/$TZ /etc/localtime

ENV DATA_DIR=/data
VOLUME /data
EXPOSE 8300

HEALTHCHECK --interval=60s --timeout=10s --start-period=15s \
  CMD python -c "import urllib.request,ssl,sys;ctx=ssl._create_unverified_context();sys.exit(0 if b'ok' in urllib.request.urlopen('https://127.0.0.1:8300/api/health',timeout=5,context=ctx).read() else 1)"

CMD ["python", "-m", "app.main"]
