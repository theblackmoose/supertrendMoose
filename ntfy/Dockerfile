FROM binwiederhier/ntfy:v2.11.0

# Must be root: the bootstrap writes generated secrets into a fresh Docker
# volume, which is created root-owned. ntfy itself runs as root in this
# image by default, so this is not a downgrade.
USER 0

# Current security fixes for the Alpine packages in the pinned image.
RUN apk upgrade --no-cache

# Baked in rather than bind-mounted: the host filesystem never touches these,
# and the sed makes the build byte-identical on Linux, macOS and Windows.
COPY bootstrap.sh /etc/ntfy/bootstrap.sh
COPY moose-creds /usr/local/bin/moose-creds
RUN sed -i 's/\r$//' /etc/ntfy/bootstrap.sh /usr/local/bin/moose-creds \
    && chmod +x /etc/ntfy/bootstrap.sh /usr/local/bin/moose-creds

ENTRYPOINT []
CMD ["/bin/sh", "/etc/ntfy/bootstrap.sh"]
