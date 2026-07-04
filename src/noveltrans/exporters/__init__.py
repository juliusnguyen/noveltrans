"""Exporter registry."""

from __future__ import annotations

from noveltrans.errors import ExportError
from noveltrans.exporters.base import Exporter


def get_exporter(name: str) -> Exporter:
    if name == "markdown":
        from noveltrans.exporters.markdown import MarkdownExporter

        return MarkdownExporter()
    if name == "docx":
        from noveltrans.exporters.docx import DocxExporter

        return DocxExporter()
    if name == "epub":
        from noveltrans.exporters.epub import EpubExporter

        return EpubExporter()
    raise ExportError(f"Unknown exporter: {name!r}")


EXPORTER_NAMES = {
    "docx": "Word (.docx)",
    "markdown": "Markdown (.md)",
    "epub": "EPUB (.epub)",
}
