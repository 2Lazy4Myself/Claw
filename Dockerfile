FROM python:3.11-alpine

RUN apk add --no-cache tzdata

ENV TZ=Europe/London

WORKDIR /app

# Install production dependencies directly (no editable install needed in prod)
RUN pip install --no-cache-dir \
    anthropic>=0.25.0 \
    requests>=2.31.0 \
    python-dotenv>=1.0.0 \
    pyyaml>=6.0

COPY claw/ claw/

RUN mkdir -p /logs /app/data /app/config

# BusyBox crond reads per-user crontabs from /var/spool/cron/crontabs/
COPY docker/crontab /var/spool/cron/crontabs/root
RUN chmod 600 /var/spool/cron/crontabs/root

CMD ["crond", "-f", "-l", "2"]
