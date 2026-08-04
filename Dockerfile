FROM python:3.12-slim

WORKDIR /app
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 BUGBOT_DB=/data/state.db

# Зависимостей две, ставим их отдельным слоем — код меняется чаще.
RUN pip install --no-cache-dir "httpx>=0.28.1" "python-dotenv>=1.0.1"

COPY bugbot ./bugbot
RUN mkdir -p /data

CMD ["python", "-m", "bugbot"]
