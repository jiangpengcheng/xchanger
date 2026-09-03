FROM python:3.13-alpine

WORKDIR /app

COPY server.py /app/server.py
COPY data/apps.json /app/data/apps.json

RUN addgroup -S app && adduser -S -G app app && mkdir -p /app/apks

USER app

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
  CMD ["python3", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=2).read()"]

ENTRYPOINT ["python3", "/app/server.py", "--host", "0.0.0.0", "--port", "8080"]
CMD ["--public-base-url", "http://api.xchanger.cn", "--upstream-base-url", "https://api.xchanger.cn"]
