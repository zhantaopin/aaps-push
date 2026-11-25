FROM python:3.11-slim

ENV TZ=Asia/Shanghai \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY app/ app/
COPY healthcheck.py /healthcheck.py
RUN mkdir -p /data && ln -snf /usr/share/zoneinfo/$TZ /etc/localtime

ENV DATA_DIR=/data
VOLUME /data
EXPOSE 8300

HEALTHCHECK --interval=60s --timeout=10s --start-period=15s \
  CMD python /healthcheck.py

CMD ["python", "-m", "app.main"]
