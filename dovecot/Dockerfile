FROM debian:bookworm-slim

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y --no-install-recommends \
        dovecot-core \
        dovecot-imapd \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/* /etc/dovecot/conf.d /etc/dovecot/dovecot.conf

COPY dovecot.conf.template /etc/dovecot/dovecot.conf.template
COPY entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod 0755 /usr/local/bin/entrypoint.sh

VOLUME ["/srv/mail"]
EXPOSE 143

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD bash -c 'exec 3<>/dev/tcp/127.0.0.1/143 && head -n1 <&3 | grep -q OK'

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
CMD ["dovecot", "-F"]
