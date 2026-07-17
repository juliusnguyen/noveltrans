"""AudioTab text-preview: reproduces exactly what the engine will be handed.

_engine_text_for mirrors AudioWorker's source-selection + synthesize_chapter's
title/body join + clean toggle, so a drift between preview and reality would show here.
"""

from __future__ import annotations

from PySide6.QtCore import QSettings

from noveltrans.config import AppConfig
from noveltrans.gui.tab_audio import AudioTab
from noveltrans.models import Chapter


def _config(tmp_path, *, use_translation: bool, clean: bool, extra_remove: str = "") -> AppConfig:
    config = AppConfig()
    config._s = QSettings(str(tmp_path / "s.ini"), QSettings.Format.IniFormat)
    config.tts_use_translation = use_translation
    config.tts_clean_text = clean
    config.tts_clean_extra_remove = extra_remove
    return config


def _chapter() -> Chapter:
    return Chapter(
        index=0,
        title="★ Nguyên tác ★",
        url="https://x/0",
        content="Bản gốc 中文 ở đây。",
        translated="Bản dịch 😀 ở đây！",
        translated_title="【Chương 1】",
    )


def test_preview_uses_translated_source_and_cleans(qapp, tmp_path):
    tab = AudioTab(_config(tmp_path, use_translation=True, clean=True))
    title, text, cleaned = tab._engine_text_for(_chapter())
    assert cleaned is True
    assert title == "【Chương 1】"  # translated title chosen
    assert "😀" not in text and "★" not in text  # specials stripped
    assert text.startswith("Chương 1")  # 【】 stripped from the title line
    assert "Bản dịch ở đây!" in text  # fullwidth ！ normalised to !, emoji gone


def test_preview_uses_original_source_when_selected(qapp, tmp_path):
    tab = AudioTab(_config(tmp_path, use_translation=False, clean=True))
    _title, text, _cleaned = tab._engine_text_for(_chapter())
    assert "Bản gốc" in text  # original content, not the translation
    assert "中" not in text  # leftover CJK stripped
    assert text.rstrip().endswith("ở đây.")  # 。 normalised to .


def test_preview_shows_raw_text_when_cleaning_off(qapp, tmp_path):
    tab = AudioTab(_config(tmp_path, use_translation=True, clean=False))
    _title, text, cleaned = tab._engine_text_for(_chapter())
    assert cleaned is False
    assert "😀" in text and "！" in text  # nothing stripped — this is the escape hatch


def test_preview_applies_extra_remove_from_settings(qapp, tmp_path):
    # The user's "bỏ thêm ký tự" list reaches the preview (and thus the engine).
    chapter = Chapter(index=0, title="", url="u", translated="Câu (một) hai！")
    tab = AudioTab(_config(tmp_path, use_translation=True, clean=True, extra_remove="()"))
    _title, text, _cleaned = tab._engine_text_for(chapter)
    assert "(" not in text and ")" not in text  # user-listed parens gone
    assert "một" in text and text.rstrip().endswith("!")  # rest intact, ！ normalised


def test_style_combo_lists_three_styles_and_loads_config(qapp, tmp_path):
    config = _config(tmp_path, use_translation=True, clean=True)
    config.tts_style = "tin_tuc"
    tab = AudioTab(config)
    styles = [tab.style_combo.itemData(i) for i in range(tab.style_combo.count())]
    assert styles == ["tu_nhien", "doc_truyen", "tin_tuc"]
    assert tab.style_combo.currentData() == "tin_tuc"  # loaded from config


def test_voice_labels_drop_the_style_suffix(qapp, tmp_path):
    # Style is its own dropdown now — the voice label shouldn't repeat it.
    tab = AudioTab(_config(tmp_path, use_translation=True, clean=True))
    tab._on_voices_listed([("Ngọc Linh — Nữ · Bắc · Phong cách kể chuyện", "Ngọc Linh")])
    assert tab.voice_combo.itemText(0) == "Ngọc Linh — Nữ · Bắc"
    assert tab.voice_combo.itemData(0) == "Ngọc Linh"  # voice id unchanged
