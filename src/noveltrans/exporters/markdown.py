"""Markdown exporter — front matter + one `##` section per chapter."""

from __future__ import annotations

from pathlib import Path

from noveltrans.errors import ExportError
from noveltrans.exporters.base import Exporter, meta_text
from noveltrans.models import Chapter, NovelMeta


class MarkdownExporter(Exporter):
    name = "markdown"
    display_name = "Markdown (.md)"
    extension = ".md"

    def export(
        self,
        meta: NovelMeta,
        chapters: list[Chapter],
        out_path: Path,
        use_translation: bool = True,
    ) -> Path:
        included, skipped = self.split_available(chapters, use_translation)
        if not included:
            raise ExportError("Không có chương nào để xuất (chưa tải/dịch chương nào).")

        title, description = meta_text(meta, use_translation)
        lines: list[str] = [f"# {title}", ""]
        if use_translation and meta.translated_title:
            lines += [f"*Tên gốc: {meta.title}*", ""]
        if meta.author:
            lines += [f"**Tác giả:** {meta.author}", ""]
        if description:
            lines += [f"> {description.replace(chr(10), chr(10) + '> ')}", ""]
        lines += [f"*Nguồn: {meta.url} — xuất bởi NovelTrans*", ""]
        if skipped:
            names = ", ".join(c.title for c in skipped[:10])
            more = "…" if len(skipped) > 10 else ""
            lines += [f"*Bỏ qua {len(skipped)} chương chưa có nội dung: {names}{more}*", ""]

        for _chapter, text in included:
            lines += [f"## {text.title}", "", text.body, ""]

        out_path = Path(out_path)
        out_path.write_text("\n".join(lines), encoding="utf-8")
        return out_path
