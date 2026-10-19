#!/bin/sh
# Container entrypoint, also used by the HEALTHCHECK so that runs unprivileged
# too. Without PITBOSS_RUN_AS, or when not root, it just runs the command.
#
# The compose files start the container as root with PITBOSS_RUN_AS set:
# docker-compose.yml to the image's `pitboss` user (10001), and
# docker-compose.ble.yml to `nobody` (65534), because the host's D-Bus refuses
# uids the host has no user for. Both share the pitboss-data volume, so hand
# /data to the configured uid whenever something there belongs to another
# one (switching variants, or root-owned files from the old Bluetooth setup),
# then drop root for good.
set -e

if [ "$(id -u)" = 0 ] && [ -n "$PITBOSS_RUN_AS" ]; then
    uid=${PITBOSS_RUN_AS%%:*}
    gid=${PITBOSS_RUN_AS#*:}
    [ "$gid" = "$PITBOSS_RUN_AS" ] && gid=$uid
    if [ -n "$(find /data \( ! -user "$uid" -o ! -group "$gid" \) -print -quit)" ]; then
        chown -R "$uid:$gid" /data
    fi
    exec setpriv --reuid="$uid" --regid="$gid" --clear-groups --no-new-privs -- "$@"
fi

exec "$@"
