from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any
from urllib.parse import urljoin, urlparse

import aiohttp

from kumacord.models import DashboardSnapshot, MonitorSnapshot, MonitorStatus

STATUS_TO_ENUM = {
    0: MonitorStatus.DOWN,
    1: MonitorStatus.UP,
    2: MonitorStatus.PENDING,
    3: MonitorStatus.MAINTENANCE,
}


class ProviderError(RuntimeError):
    """Base provider error for recoverable fetch/auth/parse failures."""


class AuthenticationError(ProviderError):
    """Authentication failed for authenticated provider mode."""


class DataFormatError(ProviderError):
    """Provider returned unexpected payload shape."""


@dataclass(slots=True)
class StatusPageMetadata:
    title: str
    icon_url: str | None
    banner_url: str | None


class KumaDataProvider(ABC):
    @abstractmethod
    async def fetch_snapshot(self) -> DashboardSnapshot:
        raise NotImplementedError


class StatusPageAdapter(KumaDataProvider):
    """Reads public status page metadata + live heartbeat data."""

    def __init__(self, statuspage_url: str, session: aiohttp.ClientSession) -> None:
        self.statuspage_url = statuspage_url.rstrip("/")
        self.slug = self._extract_slug(statuspage_url)
        self.base_url = self._extract_base_url(statuspage_url)
        self.session = session

    @staticmethod
    def _extract_slug(url: str) -> str:
        path = urlparse(url).path.strip("/")
        parts = path.split("/")
        if len(parts) < 2 or parts[-2] != "status" or not parts[-1]:
            raise ValueError("KUMA_STATUSPAGE_URL must look like https://host/status/<slug>")
        return parts[-1]

    @staticmethod
    def _extract_base_url(url: str) -> str:
        parsed = urlparse(url)
        if not parsed.scheme or not parsed.netloc:
            raise ValueError("KUMA_STATUSPAGE_URL must include a valid scheme and host")
        return f"{parsed.scheme}://{parsed.netloc}"

    async def _safe_get_json(self, url: str) -> dict[str, Any]:
        try:
            async with self.session.get(url, timeout=20) as response:
                response.raise_for_status()
                payload = await response.json()
        except aiohttp.ClientResponseError as exc:
            raise ProviderError(f"Status page request failed ({exc.status}) for {url}") from exc
        except (aiohttp.ClientError, TimeoutError) as exc:
            raise ProviderError(f"Status page request failed for {url}: {exc}") from exc

        if not isinstance(payload, dict):
            raise DataFormatError(f"Expected object payload from {url}")
        return payload

    async def _scrape_open_graph(self) -> tuple[str | None, str | None]:
        try:
            async with self.session.get(self.statuspage_url, timeout=20) as response:
                response.raise_for_status()
                html = await response.text()
        except aiohttp.ClientError:
            return None, None

        image: str | None = None
        title: str | None = None
        for line in html.splitlines():
            line_stripped = line.strip()
            if 'property="og:image"' in line_stripped and 'content="' in line_stripped:
                image = line_stripped.split('content="', 1)[1].split('"', 1)[0]
            if 'property="og:title"' in line_stripped and 'content="' in line_stripped:
                title = line_stripped.split('content="', 1)[1].split('"', 1)[0]
        if image:
            image = urljoin(self.base_url, image)
        return image, title

    async def _read_metadata(self) -> tuple[dict[str, Any], dict[str, Any], StatusPageMetadata]:
        meta_url = f"{self.base_url}/api/status-page/{self.slug}"
        hb_url = f"{self.base_url}/api/status-page/heartbeat/{self.slug}"
        meta = await self._safe_get_json(meta_url)
        heartbeat = await self._safe_get_json(hb_url)
        og_image, og_title = await self._scrape_open_graph()

        title = (
            meta.get("config", {}).get("title")
            or meta.get("title")
            or og_title
            or f"Uptime Kuma ({self.slug})"
        )
        icon = meta.get("config", {}).get("icon") or meta.get("icon")
        if icon:
            icon = urljoin(self.base_url, icon)

        banner = (
            meta.get("config", {}).get("logo")
            or meta.get("config", {}).get("image")
            or meta.get("logo")
            or og_image
        )
        if banner:
            banner = urljoin(self.base_url, banner)

        return meta, heartbeat, StatusPageMetadata(title=title, icon_url=icon, banner_url=banner)

    async def fetch_snapshot(self) -> DashboardSnapshot:
        meta, heartbeat, branding = await self._read_metadata()

        monitor_lookup: dict[int, dict[str, Any]] = {}
        for group in meta.get("publicGroupList", []):
            for monitor in group.get("monitorList", []):
                monitor_id = monitor.get("id")
                if monitor_id is None:
                    continue
                monitor_lookup[int(monitor_id)] = monitor

        if not monitor_lookup:
            raise DataFormatError("No monitors found in publicGroupList")

        heartbeat_list = heartbeat.get("heartbeatList", {})
        uptime_list = heartbeat.get("uptimeList", {})
        monitors: list[MonitorSnapshot] = []

        for monitor_id_raw, monitor in sorted(monitor_lookup.items(), key=lambda item: item[1].get("name", "")):
            key = str(monitor_id_raw)
            hb_entries = heartbeat_list.get(key) or []
            latest = hb_entries[-1] if hb_entries else {}
            status_int = latest.get("status")
            status = STATUS_TO_ENUM.get(status_int, MonitorStatus.UNKNOWN)
            ping = latest.get("ping")

            uptime_payload = uptime_list.get(key)
            uptime_24h = None
            if isinstance(uptime_payload, dict):
                uptime_24h = uptime_payload.get("24")
            elif isinstance(uptime_payload, (int, float)):
                uptime_24h = float(uptime_payload)

            monitors.append(
                MonitorSnapshot(
                    monitor_id=int(monitor_id_raw),
                    name=monitor.get("name", f"Monitor {monitor_id_raw}"),
                    status=status,
                    ping_ms=float(ping) if isinstance(ping, (int, float)) else None,
                    uptime_24h=float(uptime_24h) if isinstance(uptime_24h, (int, float)) else None,
                )
            )

        return DashboardSnapshot(
            title=branding.title,
            icon_url=branding.icon_url,
            banner_url=branding.banner_url,
            monitors=monitors,
            source="statuspage",
        )


class DirectApiAdapter(KumaDataProvider):
    """Uses authenticated Uptime Kuma API mode."""

    def __init__(self, api_url: str, username: str, password: str, session: aiohttp.ClientSession) -> None:
        self.api_url = api_url.rstrip("/")
        self.username = username
        self.password = password
        self.session = session
        self._authed = False

    async def _ensure_auth(self) -> None:
        if self._authed:
            return
        try:
            async with self.session.post(
                f"{self.api_url}/api/login",
                json={"username": self.username, "password": self.password},
                timeout=20,
            ) as response:
                response.raise_for_status()
                payload = await response.json()
        except aiohttp.ClientResponseError as exc:
            if exc.status in {401, 403}:
                raise AuthenticationError("Uptime Kuma login rejected: invalid credentials") from exc
            raise ProviderError(f"Uptime Kuma login failed ({exc.status})") from exc
        except (aiohttp.ClientError, TimeoutError) as exc:
            raise ProviderError(f"Uptime Kuma login failed: {exc}") from exc

        if not isinstance(payload, dict) or payload.get("ok") is not True:
            raise AuthenticationError("Uptime Kuma login failed: malformed or unsuccessful response")
        self._authed = True

    async def fetch_snapshot(self) -> DashboardSnapshot:
        await self._ensure_auth()

        try:
            async with self.session.get(f"{self.api_url}/api/monitors", timeout=20) as response:
                response.raise_for_status()
                payload = await response.json()
        except aiohttp.ClientResponseError as exc:
            raise ProviderError(f"Failed to fetch /api/monitors ({exc.status})") from exc
        except (aiohttp.ClientError, TimeoutError) as exc:
            raise ProviderError(f"Failed to fetch /api/monitors: {exc}") from exc

        raw_monitors: list[dict[str, Any]]
        if isinstance(payload, list):
            raw_monitors = payload
        elif isinstance(payload, dict) and isinstance(payload.get("monitorList"), list):
            raw_monitors = payload["monitorList"]
        else:
            raise DataFormatError("Unexpected /api/monitors payload format")

        monitors: list[MonitorSnapshot] = []
        for monitor in raw_monitors:
            status_int = monitor.get("active")
            status = MonitorStatus.UP if status_int else MonitorStatus.DOWN
            monitors.append(
                MonitorSnapshot(
                    monitor_id=int(monitor.get("id", 0)),
                    name=monitor.get("name", "Unnamed monitor"),
                    status=status,
                    ping_ms=None,
                    uptime_24h=None,
                )
            )

        return DashboardSnapshot(
            title="Uptime Kuma",
            icon_url=None,
            banner_url=None,
            monitors=sorted(monitors, key=lambda m: m.name.lower()),
            source="direct-api",
        )


def build_provider(
    statuspage_url: str | None,
    api_url: str | None,
    username: str | None,
    password: str | None,
    session: aiohttp.ClientSession,
) -> KumaDataProvider:
    if api_url and username and password:
        return DirectApiAdapter(api_url=api_url, username=username, password=password, session=session)
    if statuspage_url:
        return StatusPageAdapter(statuspage_url=statuspage_url, session=session)
    raise ValueError(
        "Configuration missing: provide KUMA_STATUSPAGE_URL or (KUMA_API_URL, KUMA_USERNAME, KUMA_PASSWORD)."
    )
