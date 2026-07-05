# Watchdog — the self-improvement-loop self-watch. One pass per invocation
# (python -m watchdog.watchdog); wire it to a ~10-minute cron. Stdlib + psycopg2
# only. Does nothing unless feature_flags.AGENT_EVENTS_ENABLED is true.

FROM python:3.14-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

COPY requirements.txt /tmp/requirements.txt
RUN pip install -r /tmp/requirements.txt

RUN useradd --uid 1001 --create-home appuser
# The package lands at /app/watchdog so `python -m watchdog.watchdog` resolves
# its relative imports (the in-repo path is services/watchdog).
COPY --chown=appuser:appuser . /app/watchdog
USER appuser

CMD ["python", "-m", "watchdog.watchdog"]
