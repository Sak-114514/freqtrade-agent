FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    FREQTRADE_AGENT_HOST=0.0.0.0 \
    FREQTRADE_AGENT_PORT=8090 \
    FREQTRADE_AGENT_USER_DATA_DIR=/app/user_data

WORKDIR /app

COPY pyproject.toml README.md ./
COPY tools ./tools

RUN pip install --no-cache-dir .

EXPOSE 8090

CMD ["freqtrade-agent"]
