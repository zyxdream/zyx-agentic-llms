FROM debian:bookworm-slim

ENV DEBIAN_FRONTEND=noninteractive PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
RUN apt-get update \
    && apt-get install --no-install-recommends -y python3 python3-venv python3-uno libreoffice-calc libreoffice-writer fonts-liberation ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && python3 -m venv --system-site-packages /opt/python
COPY requirements.txt /opt/runtime/requirements.txt
RUN /opt/python/bin/pip install --no-cache-dir -r /opt/runtime/requirements.txt
COPY bootstrap.py helpers.py /opt/runtime/
COPY document_to_pdf.py /opt/runtime/
USER 65532:65532
WORKDIR /scratch
ENTRYPOINT ["/usr/bin/timeout", "--signal=TERM", "--kill-after=3s"]
CMD ["120s", "/opt/python/bin/python", "-B", "/opt/runtime/bootstrap.py"]
