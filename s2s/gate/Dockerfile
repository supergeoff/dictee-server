FROM python:3.13.18-slim AS base

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY gate.py .

# Étape de test : `docker build --target test` (make s2s-check). Dokploy construit l'étape finale, runtime.
FROM base AS test
COPY requirements-test.txt .
RUN pip install --no-cache-dir -r requirements-test.txt
COPY tests tests
CMD ["python", "-m", "pytest", "-q", "tests"]

FROM base AS runtime
RUN useradd --no-create-home --uid 10001 gate
USER gate
EXPOSE 8080
CMD ["python", "gate.py"]
