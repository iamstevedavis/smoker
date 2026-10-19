#!/bin/sh
# Container entrypoint. Normally a no-op: the image runs as `pitboss`.
#
# docker-compose.ble.yml starts the container as root with PITBOSS_RUN_AS set
# (default `nobody`, 65534). The host's D-Bus refuses connections from uids
# the host has no user for, and the image's uid 10001 usually has none, while
# `nobody` exists on every Linux host. So hand /data to that uid (volumes
# from the old root-run Bluetooth setup hold root-owned files), then drop
# root for good before the bridge starts.
set -e

if [ "$(id -u)" = 0 ] && [ -n "$PITBOSS_RUN_AS" ]; then
    uid=${PITBOSS_RUN_AS%%:*}
    gid=${PITBOSS_RUN_AS#*:}
    [ "$gid" = "$PITBOSS_RUN_AS" ] && gid=$uid
    chown -R "$uid:$gid" /data
    exec setpriv --reuid="$uid" --regid="$gid" --clear-groups --no-new-privs -- "$@"
fi

exec "$@"
