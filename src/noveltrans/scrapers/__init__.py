"""Scraper registry: map a novel URL to its SiteAdapter."""

from __future__ import annotations

from noveltrans.scrapers.base import HttpClient, SiteAdapter

# Adapter classes register here; order matters only for overlapping patterns.
ADAPTERS: list[type[SiteAdapter]] = []


def register(adapter_cls: type[SiteAdapter]) -> type[SiteAdapter]:
    ADAPTERS.append(adapter_cls)
    return adapter_cls


def adapter_for_url(url: str, client: HttpClient) -> SiteAdapter | None:
    for adapter_cls in ADAPTERS:
        if adapter_cls.matches(url):
            return adapter_cls(client)
    return None


def _import_adapters() -> None:
    """Import all adapter modules so their @register decorators run."""
    from noveltrans.scrapers import giatocvuongtai, ixdzs, medoctruyen, xbanxia  # noqa: F401


_import_adapters()
