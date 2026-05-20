FROM python:3.11-alpine

RUN apk add --no-cache tzdata

ENV TZ=Europe/London

WORKDIR /app

# Install production dependencies directly (no editable install needed in prod)
RUN pip install --no-cache-dir \
    "openai>=1.0.0" \
    "requests>=2.31.0" \
    "python-dotenv>=1.0.0" \
    "pyyaml>=6.0"

COPY claw/ claw/

RUN mkdir -p /app/data /app/config

CMD ["python", "-m", "claw.main"]
