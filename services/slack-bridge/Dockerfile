# Slack bridge — Slack chat surface (Slack Events API messaging endpoint).
# Disabled by default; deployed only when the `slack_enabled` Terraform variable
# is true. Bridges inbound Slack messages to PaperClip issues and replies via
# chat.postMessage. Stateless FastAPI app behind ACA internal ingress.

FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

COPY requirements.txt /tmp/requirements.txt
RUN pip install -r /tmp/requirements.txt

RUN useradd --uid 1001 --create-home appuser
COPY --chown=appuser:appuser main.py /app/main.py
USER appuser

# Same conventional messaging port as the Teams bridge for a uniform ingress shape.
EXPOSE 3978
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "3978"]
