# Teams bridge — Microsoft Teams chat surface (Bot Framework messaging endpoint).
# Disabled by default; deployed only when the `teams_enabled` Terraform variable
# is true. Bridges inbound Teams messages to PaperClip issues and replies with
# Adaptive Cards. Stateless FastAPI app behind ACA internal ingress.

FROM python:3.14-slim

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

# Bot Framework's conventional messaging port.
EXPOSE 3978
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "3978"]
