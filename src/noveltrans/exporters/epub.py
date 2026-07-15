"""EPUB exporter via ebooklib — cover-less EPUB3 with a working TOC."""

from __future__ import annotations

import html
from pathlib import Path

from ebooklib import epub

from noveltrans.errors import ExportError
from noveltrans.exporters.base import Exporter, meta_text
from noveltrans.models import Chapter, NovelMeta

_LANG_BY_TRANSLATION = {True: None, False: "zh"}  # None -> set from target lang


class EpubExporter(Exporter):
    name = "epub"
    display_name = "EPUB (.epub)"
    extension = ".epub"

    def export(
        self,
        meta: NovelMeta,
        chapters: list[Chapter],
        out_path: Path,
        use_translation: bool = True,
        number_chapters: bool = False,
    ) -> Path:
        included, skipped = self.split_available(chapters, use_translation, number_chapters)
        if not included:
            raise ExportError("Không có chương nào để xuất (chưa tải/dịch chương nào).")

        title, description = meta_text(meta, use_translation)
        book = epub.EpubBook()
        book.set_identifier(meta.url or meta.title)
        book.set_title(title)
        language = (included[0][0].target_lang or "zh") if use_translation else "zh"
        book.set_language(language)
        if meta.author:
            book.add_author(meta.author)

        # --- intro page
        intro_html = [f"<h1>{html.escape(title)}</h1>"]
        if use_translation and meta.translated_title:
            intro_html.append(f"<p><i>Tên gốc: {html.escape(meta.title)}</i></p>")
        if meta.author:
            intro_html.append(f"<p><b>{html.escape(meta.author)}</b></p>")
        if description:
            intro_html.append(f"<p>{html.escape(description)}</p>")
        intro_html.append(f"<p><i>Nguồn: {html.escape(meta.url)} — xuất bởi NovelTrans</i></p>")
        if skipped:
            intro_html.append(f"<p><i>Bỏ qua {len(skipped)} chương chưa có nội dung.</i></p>")
        intro = epub.EpubHtml(title="Giới thiệu", file_name="intro.xhtml", lang=language)
        intro.content = "".join(intro_html)
        book.add_item(intro)

        # --- chapters
        items = [intro]
        for i, (_chapter, text) in enumerate(included):
            paragraphs = "".join(
                f"<p>{html.escape(p.strip())}</p>" for p in text.body.split("\n\n") if p.strip()
            )
            item = epub.EpubHtml(
                title=text.title, file_name=f"chap_{i + 1:04d}.xhtml", lang=language
            )
            item.content = f"<h1>{html.escape(text.title)}</h1>{paragraphs}"
            book.add_item(item)
            items.append(item)

        book.toc = items[1:]
        book.add_item(epub.EpubNcx())
        book.add_item(epub.EpubNav())
        book.spine = ["nav", *items]

        out_path = Path(out_path)
        epub.write_epub(str(out_path), book)
        return out_path
