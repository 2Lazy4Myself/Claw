FROM python:3.11-alpine

RUN apk add --no-cache tzdata

ENV TZ=Europe/London
ENV PYTHONUNBUFFERED=1

WORKDIR /app

# Install production dependencies from the pinned runtime manifest (single source
# of truth; test deps live in the [dev] extra and are not installed in prod).
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY claw/ claw/

RUN mkdir -p /app/data /app/config

CMD ["python", "-m", "claw.main"]
