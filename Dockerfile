FROM python:3.12-slim

WORKDIR /app
COPY marshrutka_watch.py /app/
COPY config.json /app/

ENV PYTHONUNBUFFERED=1
ENV TZ=Europe/Moscow

CMD ["python3", "/app/marshrutka_watch.py", "--config", "/app/config.json"]
