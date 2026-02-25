from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
import re
from typing import Any
from urllib.parse import urljoin, urlparse

import aiohttp

from kumacord.models import DashboardSnapshot, MonitorGroup, MonitorSnapshot, MonitorStatus

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
        self.slug = self._extract_slug_from_url(self.statuspage_url)
        self.base_url = self._extract_base_url(self.statuspage_url, self.slug)
        self.session = session

    @staticmethod
    def _extract_slug_from_url(url: str) -> str | None:
        path = urlparse(url).path.strip("/")
        if not path:
            return None
        parts = path.split("/")
        if len(parts) >= 2 and parts[-2] == "status" and parts[-1]:
            return parts[-1]
        return None

    @staticmethod
    def _extract_base_url(url: str, slug: str | None) -> str:
        parsed = urlparse(url)
        if not parsed.scheme or not parsed.netloc:
            raise ValueError("KUMA_STATUSPAGE_URL must include a valid scheme and host")
        base = f"{parsed.scheme}://{parsed.netloc}"
        path = (parsed.path or "").rstrip("/")

        if slug:
            parts = [part for part in path.split("/") if part]
            if len(parts) >= 2 and parts[-2] == "status" and parts[-1] == slug:
                prefix_parts = parts[:-2]
                path = f"/{'/'.join(prefix_parts)}" if prefix_parts else ""

        if path:
            return f"{base}{path}"
        return base

    def _normalize_asset_url(self, asset: str | None) -> str | None:
        if not asset:
            return None
        if asset.startswith(("http://", "https://")):
            return asset

        base_parsed = urlparse(self.base_url)
        base_path = base_parsed.path.rstrip("/")

        if asset.startswith("/") and base_path and not asset.startswith(f"{base_path}/"):
            asset = f"{base_path}{asset}"

        base = self.base_url.rstrip("/") + "/"
        return urljoin(base, asset.lstrip("/"))

    @staticmethod
    def _extract_slug_from_html(html: str) -> str | None:
        patterns = [
            r"/api/status-page/heartbeat/([a-zA-Z0-9_-]+)",
            r"/api/status-page/([a-zA-Z0-9_-]+)",
            r"statusPageSlug\"?\s*:\s*\"([^\"]+)\"",
            r"statusPageSlug\"?\s*:\s*'([^']+)'",
            r"/status/([a-zA-Z0-9_-]+)",
        ]
        for pattern in patterns:
            match = re.search(pattern, html)
            if match:
                return match.group(1)
        return None

    async def _ensure_slug(self) -> None:
        if self.slug:
            return

        try:
            async with self.session.get(self.statuspage_url, timeout=20) as response:
                response.raise_for_status()
                html = await response.text()
        except aiohttp.ClientResponseError as exc:
            raise ProviderError(f"Status page request failed ({exc.status}) for {self.statuspage_url}") from exc
        except (aiohttp.ClientError, TimeoutError) as exc:
            raise ProviderError(f"Status page request failed for {self.statuspage_url}: {exc}") from exc

        slug = self._extract_slug_from_html(html)
        if not slug:
            raise ValueError(
                "KUMA_STATUSPAGE_URL must point to the status page (https://host/status/<slug>) "
                "or a direct status page URL. Could not detect slug from HTML."
            )
        self.slug = slug

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

    async def _safe_get_manifest(self, url: str) -> dict[str, Any] | None:
        try:
            async with self.session.get(url, timeout=20) as response:
                response.raise_for_status()
                payload = await response.json()
        except aiohttp.ClientError:
            return None

        if not isinstance(payload, dict):
            return None
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
        image = self._normalize_asset_url(image)
        return image, title

    async def _read_metadata(self) -> tuple[dict[str, Any], dict[str, Any], StatusPageMetadata]:
        await self._ensure_slug()
        meta_url = f"{self.base_url}/api/status-page/{self.slug}"
        hb_url = f"{self.base_url}/api/status-page/heartbeat/{self.slug}"
        manifest_url = f"{self.base_url}/api/status-page/{self.slug}/manifest.json"
        meta = await self._safe_get_json(meta_url)
        heartbeat = await self._safe_get_json(hb_url)
        og_image, og_title = await self._scrape_open_graph()
        manifest = await self._safe_get_manifest(manifest_url)

        title = (
            meta.get("config", {}).get("title")
            or meta.get("title")
            or og_title
            or f"Uptime Kuma ({self.slug})"
        )
        icon = self._normalize_asset_url(meta.get("config", {}).get("icon") or meta.get("icon"))
        if not icon and isinstance(manifest, dict):
            icons = manifest.get("icons") or []
            if isinstance(icons, list) and icons:
                first = icons[0] if isinstance(icons[0], dict) else None
                if first and first.get("src"):
                    icon = self._normalize_asset_url(first.get("src"))

        banner = (
            meta.get("config", {}).get("logo")
            or meta.get("config", {}).get("image")
            or meta.get("logo")
            or og_image
        )
        banner = self._normalize_asset_url(banner)

        return meta, heartbeat, StatusPageMetadata(title=title, icon_url=icon, banner_url=banner)

    def _build_monitor_snapshot(
        self,
        monitor: dict[str, Any],
        monitor_id_raw: int,
        heartbeat_list: dict[str, Any],
        uptime_list: dict[str, Any],
    ) -> MonitorSnapshot:
        key = str(monitor_id_raw)
        hb_entries = heartbeat_list.get(key) or []
        latest = hb_entries[-1] if hb_entries else {}
        status_int = latest.get("status")
        status = STATUS_TO_ENUM.get(status_int, MonitorStatus.UNKNOWN)
        ping = latest.get("ping")

        uptime_payload = None
        if isinstance(uptime_list, dict):
            uptime_payload = uptime_list.get(f"{key}_24")
            if uptime_payload is None:
                uptime_payload = uptime_list.get(key)
        uptime_24h = None
        if isinstance(uptime_payload, dict):
            uptime_24h = uptime_payload.get("24")
        elif isinstance(uptime_payload, (int, float, str)):
            try:
                uptime_24h = float(uptime_payload)
            except (TypeError, ValueError):
                uptime_24h = None

        if isinstance(uptime_24h, (int, float)) and uptime_24h <= 1.0:
            uptime_24h = uptime_24h * 100

        return MonitorSnapshot(
            monitor_id=int(monitor_id_raw),
            name=monitor.get("name", f"Monitor {monitor_id_raw}"),
            status=status,
            ping_ms=float(ping) if isinstance(ping, (int, float)) else None,
            uptime_24h=float(uptime_24h) if isinstance(uptime_24h, (int, float)) else None,
        )

    async def fetch_snapshot(self) -> DashboardSnapshot:
        meta, heartbeat, branding = await self._read_metadata()

        heartbeat_list = heartbeat.get("heartbeatList", {})
        uptime_list = heartbeat.get("uptimeList", {})
        groups: list[MonitorGroup] = []
        seen_ids: set[int] = set()

        raw_groups = meta.get("publicGroupList", [])
        if isinstance(raw_groups, list) and raw_groups:
            for group in sorted(raw_groups, key=lambda item: item.get("weight", 0)):
                group_monitors: list[MonitorSnapshot] = []
                for monitor in group.get("monitorList", []):
                    monitor_id = monitor.get("id")
                    if monitor_id is None:
                        continue
                    monitor_id_int = int(monitor_id)
                    seen_ids.add(monitor_id_int)
                    group_monitors.append(
                        self._build_monitor_snapshot(monitor, monitor_id_int, heartbeat_list, uptime_list)
                    )
                if group_monitors:
                    groups.append(
                        MonitorGroup(
                            group_id=int(group.get("id")) if group.get("id") is not None else None,
                            name=str(group.get("name") or "Group"),
                            weight=int(group.get("weight") or 0),
                            monitors=group_monitors,
                        )
                    )

        raw_monitors = meta.get("publicMonitorList") or meta.get("monitorList") or []
        if isinstance(raw_monitors, list):
            ungrouped: list[MonitorSnapshot] = []
            for monitor in raw_monitors:
                monitor_id = monitor.get("id")
                if monitor_id is None:
                    continue
                monitor_id_int = int(monitor_id)
                if monitor_id_int in seen_ids:
                    continue
                ungrouped.append(self._build_monitor_snapshot(monitor, monitor_id_int, heartbeat_list, uptime_list))
            if ungrouped:
                groups.append(
                    MonitorGroup(
                        group_id=None,
                        name="Other",
                        weight=999,
                        monitors=ungrouped,
                    )
                )

        if not groups and isinstance(raw_monitors, list) and raw_monitors:
            fallback_monitors = [
                self._build_monitor_snapshot(monitor, int(monitor.get("id", 0)), heartbeat_list, uptime_list)
                for monitor in raw_monitors
                if monitor.get("id") is not None
            ]
            if fallback_monitors:
                groups.append(
                    MonitorGroup(
                        group_id=None,
                        name="Monitors",
                        weight=0,
                        monitors=fallback_monitors,
                    )
                )

        monitors: list[MonitorSnapshot] = [monitor for group in groups for monitor in group.monitors]

        if not monitors:
            raise DataFormatError("No monitors found in status page payload")

        return DashboardSnapshot(
            title=branding.title,
            icon_url=branding.icon_url,
            banner_url=branding.banner_url,
            monitors=monitors,
            groups=groups,
            source="statuspage",
        )



class MetricsApiAdapter(KumaDataProvider):
    """Uses /metrics endpoint with API key authentication."""

    def __init__(self, api_url: str, api_key: str, session: aiohttp.ClientSession) -> None:
        self.api_url = api_url.rstrip("/")
        self.api_key = api_key
        self.session = session

    def _metrics_url(self) -> str:
        return f"{self.api_url}/metrics"

    @staticmethod
    def _parse_monitor_id(raw: object, fallback: int) -> int:
        if raw is None:
            return fallback
        try:
            return int(raw)
        except (TypeError, ValueError):
            return fallback

    async def _fetch_metrics(self) -> str:
        auth = aiohttp.BasicAuth("", self.api_key)
        try:
            async with self.session.get(self._metrics_url(), timeout=20, auth=auth) as response:
                response.raise_for_status()
                return await response.text()
        except aiohttp.ClientResponseError as exc:
            raise ProviderError(f"Metrics request failed ({exc.status}) at {self._metrics_url()}") from exc
        except (aiohttp.ClientError, TimeoutError) as exc:
            raise ProviderError(f"Metrics request failed at {self._metrics_url()}: {exc}") from exc

    @staticmethod
    def _parse_labels(raw: str) -> dict[str, str]:
        labels: dict[str, str] = {}
        for match in re.finditer(r'(\w+)="((?:\\.|[^"\\])*)"', raw):
            key = match.group(1)
            value = match.group(2).replace('\\"', '"').replace("\\\\", "\\")
            labels[key] = value
        return labels

    def _parse_metrics(self, text: str) -> dict[str, dict[str, object]]:
        data: dict[str, dict[str, object]] = {}
        metric_re = re.compile(
            r"^([a-zA-Z_:][a-zA-Z0-9_:]*)(?:\{([^}]*)\})?\s+([-+]?[0-9]*\.?[0-9]+(?:[eE][-+]?[0-9]+)?)$"
        )
        for line in text.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            match = metric_re.match(line)
            if not match:
                continue
            name = match.group(1)
            labels_raw = match.group(2) or ""
            value_raw = match.group(3)
            try:
                value = float(value_raw)
            except ValueError:
                continue

            labels = self._parse_labels(labels_raw)
            monitor_id = labels.get("monitor_id") or labels.get("id")
            monitor_name = labels.get("monitor_name") or labels.get("monitor")
            if not monitor_name and monitor_id:
                monitor_name = f"Monitor {monitor_id}"
            if not monitor_name and not monitor_id:
                continue

            key = monitor_id or monitor_name
            slot = data.setdefault(
                str(key),
                {
                    "__id": monitor_id,
                    "__name": monitor_name,
                    "__type": labels.get("monitor_type"),
                },
            )

            if monitor_id and not slot.get("__id"):
                slot["__id"] = monitor_id
            if monitor_name and not slot.get("__name"):
                slot["__name"] = monitor_name
            if labels.get("monitor_type") and not slot.get("__type"):
                slot["__type"] = labels.get("monitor_type")

            window = labels.get("window")
            metric_key = f"{name}:{window}" if window else name
            slot[metric_key] = value
        return data

    def _map_uptime(self, value: float) -> float | None:
        if value < 0:
            return None
        if value <= 1.0:
            return value * 100
        if value <= 100:
            return value
        return None

    async def fetch_snapshot(self) -> DashboardSnapshot:
        text = await self._fetch_metrics()
        parsed = self._parse_metrics(text)

        monitors: list[MonitorSnapshot] = []
        sorted_metrics = sorted(
            parsed.values(),
            key=lambda item: str(item.get("__name", "")).lower(),
        )
        for idx, metrics in enumerate(sorted_metrics, start=1):
            monitor_type = metrics.get("__type")
            if monitor_type == "group":
                continue

            name = metrics.get("__name") or f"Monitor {idx}"
            monitor_id_raw = metrics.get("__id")
            monitor_id = self._parse_monitor_id(monitor_id_raw, idx)

            status_raw = metrics.get("monitor_status")
            if isinstance(status_raw, (int, float)):
                status = STATUS_TO_ENUM.get(int(status_raw), MonitorStatus.UNKNOWN)
            else:
                status = MonitorStatus.UNKNOWN

            ping = metrics.get("monitor_response_time") or metrics.get("monitor_response_time_ms")
            if not isinstance(ping, (int, float)):
                ping_window = metrics.get("monitor_response_time_seconds:1d") or metrics.get(
                    "monitor_response_time_seconds:24h"
                )
                ping = ping_window * 1000 if isinstance(ping_window, (int, float)) else None

            uptime_raw = (
                metrics.get("monitor_uptime_ratio:1d")
                or metrics.get("monitor_uptime_ratio:24h")
                or metrics.get("monitor_uptime")
                or metrics.get("monitor_uptime_percentage")
                or metrics.get("monitor_uptime_24h")
                or metrics.get("monitor_uptime24h")
            )
            uptime = self._map_uptime(float(uptime_raw)) if isinstance(uptime_raw, (int, float)) else None

            monitors.append(
                MonitorSnapshot(
                    monitor_id=monitor_id,
                    name=str(name),
                    status=status,
                    ping_ms=float(ping) if isinstance(ping, (int, float)) else None,
                    uptime_24h=uptime,
                )
            )

        if not monitors:
            raise DataFormatError("No monitors found in metrics payload")

        monitors_sorted = sorted(monitors, key=lambda m: m.name.lower())
        groups = [MonitorGroup(group_id=None, name="Monitors", weight=0, monitors=monitors_sorted)]

        return DashboardSnapshot(
            title="Uptime Kuma",
            icon_url=None,
            banner_url=None,
            monitors=monitors_sorted,
            groups=groups,
            source="metrics-api",
        )


def build_provider(
    statuspage_url: str | None,
    api_url: str | None,
    api_key: str | None,
    session: aiohttp.ClientSession,
) -> KumaDataProvider:
    if api_url and api_key:
        return MetricsApiAdapter(api_url=api_url, api_key=api_key, session=session)
    if statuspage_url:
        return StatusPageAdapter(statuspage_url=statuspage_url, session=session)
    raise ValueError("Configuration missing: provide KUMA_STATUSPAGE_URL or (KUMA_API_URL, KUMA_API_KEY).")
