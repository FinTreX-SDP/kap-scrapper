# Squid forward proxy for kap_aws.py: the scrapers send KAP requests through several of these, so KAP sees
# many IPs. CONF holds the squid configuration (see setup.sh); a proxy stops by itself after MAX_HOURS, so
# a forgotten one costs nothing for long.
FROM alpine:3.20
RUN apk add --no-cache squid
ENV MAX_HOURS=12
CMD ["sh", "-c", "printf '%s\n' \"$CONF\" > /etc/squid/squid.conf && exec timeout $((MAX_HOURS * 3600)) squid -N -f /etc/squid/squid.conf"]
