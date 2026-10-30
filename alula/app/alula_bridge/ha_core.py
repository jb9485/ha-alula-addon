"""Minimal Home Assistant Core client for add-ons (via the Supervisor proxy).

Needs `homeassistant_api: true` in config.yaml. Used only for the arm prompt:
persistent notifications, optional mobile-app actionable notifications, and
listening for the mobile app's notification-action events.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from collections.abc import Awaitable, Callable
from typing import Any, Optional

import aiohttp

_LOGGER = logging.getLogger(__name__)

REST = "http://supervisor/core/api"
WS = "ws://supervisor/core/websocket"


class HaCore:
    def __init__(self) -> None:
        self._token = os.environ.get("SUPERVISOR_TOKEN") or ""
        self._session: Optional[aiohttp.ClientSession] = None

    @property
    def available(self) -> bool:
        return bool(self._token)

    async def _sess(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                headers={"Authorization": f"Bearer {self._token}"},
                timeout=aiohttp.ClientTimeout(total=15),
            )
        return self._session

    async def close(self) -> None:
        if self._session is not None:
            await self._session.close()

    async def call_service(self, domain: str, service: str, data: dict) -> bool:
        if not self.available:
            return False
        try:
            s = await self._sess()
            async with s.post(f"{REST}/services/{domain}/{service}", json=data) as r:
                if r.status >= 400:
                    _LOGGER.warning("HA service %s.%s failed: HTTP %s %s",
                                    domain, service, r.status, (await r.text())[:200])
                    return False
                return True
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning("HA service %s.%s failed: %s", domain, service, err)
            return False

    async def listen_events(self, event_type: str,
                            handler: Callable[[dict], Awaitable[None]],
                            stop: asyncio.Event) -> None:
        """Subscribe to one HA event type forever (reconnects on failure)."""
        if not self.available:
            return
        while not stop.is_set():
            try:
                s = await self._sess()
                async with s.ws_connect(WS, heartbeat=30) as ws:
                    await ws.receive_json()  # auth_required
                    await ws.send_json({"type": "auth", "access_token": self._token})
                    msg = await ws.receive_json()
                    if msg.get("type") != "auth_ok":
                        _LOGGER.warning("HA websocket auth failed: %s", msg.get("type"))
                        await asyncio.sleep(60)
                        continue
                    await ws.send_json({"id": 1, "type": "subscribe_events",
                                        "event_type": event_type})
                    _LOGGER.debug("listening for HA %s events", event_type)
                    async for m in ws:
                        if m.type != aiohttp.WSMsgType.TEXT:
                            continue
                        data = json.loads(m.data)
                        if data.get("type") == "event":
                            try:
                                await handler(data.get("event") or {})
                            except Exception:  # noqa: BLE001
                                _LOGGER.exception("HA event handler failed")
            except asyncio.CancelledError:
                raise
            except Exception as err:  # noqa: BLE001
                _LOGGER.debug("HA websocket error: %s; reconnecting", err)
            try:
                await asyncio.wait_for(stop.wait(), timeout=10)
            except asyncio.TimeoutError:
                pass
