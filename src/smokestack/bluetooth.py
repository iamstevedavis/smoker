"""The host's Bluetooth adapters, for the settings panel's adapter list."""

from __future__ import annotations

import asyncio
import logging
import sys
from typing import Any

logger = logging.getLogger(__name__)

_ADAPTER_INTERFACE = "org.bluez.Adapter1"
_TIMEOUT_SECONDS = 2.0


async def list_adapters() -> list[dict[str, Any]]:
    """The adapters BlueZ knows, sorted by name, e.g. `hci0`, `hci1`.

    Empty when BlueZ can't be reached: off Linux, without the host's D-Bus
    socket, or with bluetoothd stopped.
    """
    if not sys.platform.startswith("linux"):
        return []
    try:
        objects = await asyncio.wait_for(_managed_objects(), _TIMEOUT_SECONDS)
    except Exception as exc:
        logger.debug("Can't list Bluetooth adapters: %s", exc)
        return []
    return adapters_from(objects)


def adapters_from(objects: dict[str, dict[str, dict[str, Any]]]) -> list[dict[str, Any]]:
    """The adapters in a BlueZ GetManagedObjects reply."""
    adapters = []
    for path, interfaces in objects.items():
        props = interfaces.get(_ADAPTER_INTERFACE)
        if props is None:
            continue
        adapters.append(
            {
                "name": path.rsplit("/", 1)[-1],
                "address": _value(props.get("Address"), ""),
                "alias": _value(props.get("Alias"), ""),
                "powered": bool(_value(props.get("Powered"), False)),
            }
        )
    return sorted(adapters, key=lambda adapter: adapter["name"])


def _value(prop: Any, default: Any) -> Any:
    # dbus_fast wraps each property in a Variant.
    if prop is None:
        return default
    return getattr(prop, "value", prop)


async def _managed_objects() -> dict[str, dict[str, dict[str, Any]]]:
    from dbus_fast import BusType, Message, MessageType
    from dbus_fast.aio import MessageBus

    bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
    try:
        reply = await bus.call(
            Message(
                destination="org.bluez",
                path="/",
                interface="org.freedesktop.DBus.ObjectManager",
                member="GetManagedObjects",
            )
        )
    finally:
        bus.disconnect()
    if reply is None or reply.message_type == MessageType.ERROR:
        raise RuntimeError(reply.body[0] if reply and reply.body else "no reply")
    return reply.body[0]
