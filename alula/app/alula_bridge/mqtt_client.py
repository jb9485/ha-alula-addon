"""Thin async MQTT wrapper (aiomqtt)."""
from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable
from typing import Any, Optional

import aiomqtt

_LOGGER = logging.getLogger(__name__)


class Mqtt:
    def __init__(
        self,
        host: str,
        port: int = 1883,
        username: Optional[str] = None,
        password: Optional[str] = None,
        client_id: str = "alula_bridge",
    ) -> None:
        self._host = host
        self._port = port
        self._username = username
        self._password = password
        self._client_id = client_id
        self._client: Optional[aiomqtt.Client] = None
        self._handlers: dict[str, Callable[[str, str], Awaitable[None]]] = {}

    async def __aenter__(self) -> "Mqtt":
        self._client = aiomqtt.Client(
            hostname=self._host,
            port=self._port,
            username=self._username,
            password=self._password,
            identifier=self._client_id,
        )
        await self._client.__aenter__()
        return self

    async def __aexit__(self, *exc) -> None:
        if self._client is not None:
            await self._client.__aexit__(*exc)
            self._client = None

    async def publish(self, topic: str, payload: Any, *, retain: bool = True) -> None:
        assert self._client is not None
        if not isinstance(payload, (str, bytes)):
            payload = json.dumps(payload, separators=(",", ":"))
        await self._client.publish(topic, payload=payload, qos=1, retain=retain)

    async def subscribe(self, topic: str, handler: Callable[[str, str], Awaitable[None]]) -> None:
        assert self._client is not None
        await self._client.subscribe(topic)
        self._handlers[topic] = handler

    async def listen(self) -> None:
        assert self._client is not None
        async for message in self._client.messages:
            topic = str(message.topic)
            payload = message.payload.decode() if isinstance(message.payload, (bytes, bytearray)) else str(message.payload)
            for pattern, handler in list(self._handlers.items()):
                if message.topic.matches(pattern):
                    try:
                        await handler(topic, payload)
                    except Exception:  # noqa: BLE001
                        _LOGGER.exception("MQTT handler failed for %s", topic)
