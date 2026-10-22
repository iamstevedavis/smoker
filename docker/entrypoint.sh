#!/bin/sh
# Container entrypoint, also used by the HEALTHCHECK so that runs unprivileged
# too. Without SMOKESTACK_RUN_AS, or when not root, it just runs the command.
#
# docker-compose.yml starts the container as root with SMOKESTACK_RUN_AS set,
# by default to `nobody` (65534), because the host's D-Bus refuses uids the
# host has no user for. Hand /data to the configured uid whenever something
# there belongs to another one (a changed SMOKESTACK_RUN_AS, or a volume from
# the old separate WiFi or root-run Bluetooth setups), then drop root for good.
set -e

if [ "$(id -u)" = 0 ] && [ -n "$SMOKESTACK_RUN_AS" ]; then
    uid=${SMOKESTACK_RUN_AS%%:*}
    gid=${SMOKESTACK_RUN_AS#*:}
    [ "$gid" = "$SMOKESTACK_RUN_AS" ] && gid=$uid
    if [ -n "$(find /data \( ! -user "$uid" -o ! -group "$gid" \) -print -quit)" ]; then
        chown -R "$uid:$gid" /data
    fi
    exec setpriv --reuid="$uid" --regid="$gid" --clear-groups --no-new-privs -- "$@"
fi

exec "$@"
