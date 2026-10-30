"""Thin async MQTT wrapper (aiomqtt)."""
from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from typing import Any, Optional

import aiomqtt

_LOGGER = logging.getLogger(__name__)

MqttError = aiomqtt.MqttError


class Mqtt:
    """One broker connection. Handlers run as their own tasks so a slow command
    (an arm waiting on the cloud) never blocks the next message, e.g. a DISARM."""

    def __init__(
        self,
        host: str,
        port: int = 1883,
        username: Optional[str] = None,
        password: Optional[str] = None,
        client_id: str = "alula_bridge",
        will_topic: Optional[str] = None,
    ) -> None:
        self._host = host
        self._port = port
        self._username = username
        self._password = password
        self._client_id = client_id
        self._will_topic = will_topic
        self._client: Optional[aiomqtt.Client] = None
        self._handlers: dict[str, Callable[[str, str], Awaitable[None]]] = {}
        self._tasks: set[asyncio.Task] = set()

    async def __aenter__(self) -> "Mqtt":
        will = None
        if self._will_topic:
            # Broker publishes this if we vanish without a clean disconnect, so HA
            # marks every entity unavailable instead of showing stale state.
            will = aiomqtt.Will(self._will_topic, payload="offline", qos=1, retain=True)
        self._client = aiomqtt.Client(
            hostname=self._host,
            port=self._port,
            username=self._username,
            password=self._password,
            identifier=self._client_id,
            will=will,
            keepalive=30,
        )
        await self._client.__aenter__()
        return self

    async def __aexit__(self, *exc) -> None:
        for t in list(self._tasks):
            t.cancel()
        if self._client is not None:
            try:
                await self._client.__aexit__(*exc)
            finally:
                self._client = None

    async def publish(self, topic: str, payload: Any, *, retain: bool = True) -> None:
        if self._client is None:
            raise MqttError("not connected")
        if not isinstance(payload, (str, bytes)):
            payload = json.dumps(payload, separators=(",", ":"))
        await self._client.publish(topic, payload=payload, qos=1, retain=retain)

    async def subscribe(self, topic: str, handler: Callable[[str, str], Awaitable[None]]) -> None:
        if self._client is None:
            raise MqttError("not connected")
        await self._client.subscribe(topic)
        self._handlers[topic] = handler

    def _spawn(self, coro: Awaitable[None], topic: str) -> None:
        async def _run() -> None:
            try:
                await coro
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                _LOGGER.exception("MQTT handler failed for %s", topic)

        task = asyncio.create_task(_run())
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def listen(self) -> None:
        """Dispatch messages until the connection drops (raises MqttError)."""
        if self._client is None:
            raise MqttError("not connected")
        async for message in self._client.messages:
            topic = str(message.topic)
            payload = (message.payload.decode(errors="replace")
                       if isinstance(message.payload, (bytes, bytearray)) else str(message.payload))
            if message.retain:
                # Never act on a retained command (e.g. a stale DISARM left on the broker).
                _LOGGER.debug("ignoring retained message on %s", topic)
                continue
            for pattern, handler in list(self._handlers.items()):
                if message.topic.matches(pattern):
                    self._spawn(handler(topic, payload), topic)
