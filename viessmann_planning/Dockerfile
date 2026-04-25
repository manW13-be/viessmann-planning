FROM ghcr.io/home-assistant/aarch64-base:latest

RUN apk add --no-cache python3 py3-pip jq curl && \
    pip3 install --no-cache-dir flask requests PyViCare

COPY viessmann-planning-run.py /viessmann-planning-run.py
COPY viessmann-planning-cfg.py /viessmann-planning-cfg.py
COPY run.sh /run.sh
COPY templates/ /templates/

RUN chmod +x /run.sh

CMD ["/run.sh", "--loop"]
