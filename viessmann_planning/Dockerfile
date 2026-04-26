FROM python:3.12-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends jq curl \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir flask requests PyViCare

COPY viessmann-planning-run.py /viessmann-planning-run.py
COPY viessmann-planning-cfg.py /viessmann-planning-cfg.py
COPY run.sh /run.sh
COPY templates/ /templates/
COPY config.json /config.json.addon

RUN chmod +x /run.sh

CMD ["/run.sh", "--loop"]
