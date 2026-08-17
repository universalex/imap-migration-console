FROM python:3.12-slim-bookworm

ARG IMAPSYNC_VERSION=imapsync-2.314
ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# imapsync is a single Perl script; everything below is its dependency list
# (see the INSTALL doc of the imapsync project) plus curl for the download.
RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
        perl \
        procps \
        libauthen-ntlm-perl \
        libcgi-pm-perl \
        libcrypt-openssl-rsa-perl \
        libdata-uniqid-perl \
        libdigest-hmac-perl \
        libdist-checkconflicts-perl \
        libencode-imaputf7-perl \
        libfile-copy-recursive-perl \
        libfile-tail-perl \
        libio-compress-perl \
        libio-socket-inet6-perl \
        libio-socket-ssl-perl \
        libio-tee-perl \
        libjson-webtoken-perl \
        libmail-imapclient-perl \
        libmodule-scandeps-perl \
        libnet-ssleay-perl \
        libpar-packer-perl \
        libproc-processtable-perl \
        libreadonly-perl \
        libregexp-common-perl \
        libsys-meminfo-perl \
        libterm-readkey-perl \
        libtest-mockobject-perl \
        libtest-pod-perl \
        libunicode-string-perl \
        liburi-perl \
        libwww-perl \
    && rm -rf /var/lib/apt/lists/*

RUN curl -fsSL "https://raw.githubusercontent.com/imapsync/imapsync/${IMAPSYNC_VERSION}/imapsync" \
        -o /usr/local/bin/imapsync \
    && chmod 0755 /usr/local/bin/imapsync \
    && imapsync --version

WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY imapbackup ./imapbackup

ENV DATA_DIR=/data \
    MAILDIR_ROOT=/srv/mail

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8000/api/health || exit 1

CMD ["uvicorn", "imapbackup.main:app", "--host", "0.0.0.0", "--port", "8000", \
     "--proxy-headers", "--no-access-log"]
