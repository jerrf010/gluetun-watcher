FROM docker:27-cli

RUN apk add --no-cache python3 py3-pip && \
    pip install --no-cache-dir docker==7.1.0 --break-system-packages

WORKDIR /app

COPY gluetun_watcher.py .

CMD ["python3", "-u", "gluetun_watcher.py"]