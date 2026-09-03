"""Feature 077 — a chapter knows when its audio no longer matches its text.

Editing a translation in the Dịch tab left the generated audio narrating the old text,
with the Audio tab still reporting "Đã tạo" and bulk runs skipping it. The mechanism is a
fingerprint of what was voiced, taken at generation time and compared against what the
chapter says now — deliberately not a flag set by each edit site, because the text reaches
the DB through seven different writers today and none of them would have to remember this.
"""

from __future__ import annotations

from noveltrans.models import (
    AUDIO_SOURCE_DOWNLOADED,
    AUDIO_SOURCE_ORIGINAL,
    AUDIO_SOURCE_TRANSLATED,
    Chapter,
)


def _voiced(**overrides) -> Chapter:
    """A chapter whose audio was generated from its current text — i.e. fresh."""
    fields = {
        "index": 0,
        "title": "第1章 重生",
        "url": "u",
        "content": "原文。",
        "translated": "Bản dịch.",
        "translated_title": "Chương 1: Trùng sinh",
        "audio_path": "exports/audio/0001-chuong-1.wav",
        "audio_voice": "Ngọc Lan",
        "audio_source": AUDIO_SOURCE_TRANSLATED,
    }
    chapter = Chapter(**{**fields, **overrides})
    chapter.audio_text_hash = chapter.audio_fingerprint()
    return chapter


class TestSourceText:
    def test_the_translated_pair_is_title_and_body(self):
        chapter = _voiced()
        assert chapter.audio_source_text(True) == ("Chương 1: Trùng sinh", "Bản dịch.")

    def test_the_original_pair_is_the_source_title_and_content(self):
        assert _voiced().audio_source_text(False) == ("第1章 重生", "原文。")

    def test_a_missing_translated_title_falls_back_to_the_source_title(self):
        chapter = _voiced(translated_title="")
        assert chapter.audio_source_text(True)[0] == "第1章 重生"

    def test_none_means_whatever_this_row_was_voiced_from(self):
        # The row, not the tab's current radio button: a chapter voiced from the original
        # must not be judged against the translation.
        chapter = _voiced(audio_source=AUDIO_SOURCE_ORIGINAL)
        assert chapter.audio_source_text() == chapter.audio_source_text(False)


class TestFingerprint:
    def test_the_title_is_part_of_it(self):
        # synthesize_chapter speaks the title aloud, so a title edit invalidates audio
        # exactly as a body edit does.
        before = _voiced()
        after = _voiced(translated_title="Chương 1: Tên khác")
        assert before.audio_fingerprint() != after.audio_fingerprint()

    def test_the_body_is_part_of_it(self):
        assert _voiced().audio_fingerprint() != _voiced(translated="Khác.").audio_fingerprint()

    def test_the_two_halves_cannot_be_confused(self):
        # ("ab", "c") must not hash like ("a", "bc") — hence the separator byte.
        left = Chapter(index=0, title="x", url="u", translated_title="ab", translated="c")
        right = Chapter(index=0, title="x", url="u", translated_title="a", translated="bc")
        assert left.audio_fingerprint() != right.audio_fingerprint()

    def test_it_is_stable_across_calls(self):
        chapter = _voiced()
        assert chapter.audio_fingerprint() == chapter.audio_fingerprint()

    def test_the_two_sources_fingerprint_differently(self):
        chapter = _voiced()
        assert chapter.audio_fingerprint(True) != chapter.audio_fingerprint(False)


class TestIsStale:
    def test_freshly_voiced_audio_is_not_stale(self):
        assert not _voiced().audio_is_stale

    def test_editing_the_body_makes_it_stale(self):
        chapter = _voiced()
        chapter.translated = "Bản dịch đã sửa."
        assert chapter.audio_is_stale

    def test_editing_the_title_makes_it_stale(self):
        chapter = _voiced()
        chapter.translated_title = "Chương 1: Đã đổi tên"
        assert chapter.audio_is_stale

    def test_editing_the_original_makes_an_original_voiced_row_stale(self):
        chapter = _voiced(audio_source=AUDIO_SOURCE_ORIGINAL)
        chapter.content = "原文改。"
        assert chapter.audio_is_stale

    def test_editing_the_translation_leaves_an_original_voiced_row_alone(self):
        # It was never made from the translation, so a translation edit says nothing.
        chapter = _voiced(audio_source=AUDIO_SOURCE_ORIGINAL)
        chapter.translated = "Hoàn toàn khác."
        assert not chapter.audio_is_stale

    def test_editing_and_undoing_leaves_it_fresh(self):
        # The case a "needs audio" flag set on every edit would get wrong.
        chapter = _voiced()
        original = chapter.translated
        chapter.translated = "tạm"
        chapter.translated = original
        assert not chapter.audio_is_stale

    def test_a_chapter_with_no_audio_is_never_stale(self):
        chapter = _voiced(audio_path="")
        chapter.translated = "Đã sửa."
        assert not chapter.audio_is_stale  # it is simply pending, which is a different thing

    def test_audio_predating_fingerprints_is_never_stale(self):
        # Every row in every existing library has an empty hash. Treating those as stale
        # would tell the user their whole novel needs re-voicing.
        chapter = _voiced()
        chapter.audio_text_hash = ""
        chapter.translated = "Đã sửa."
        assert not chapter.audio_is_stale

    def test_downloaded_narration_is_never_stale(self):
        # A different edition, not a render of this text — editing the translation cannot
        # make the site's own recording wrong.
        chapter = _voiced(audio_source=AUDIO_SOURCE_DOWNLOADED)
        chapter.translated = "Đã sửa."
        assert not chapter.audio_is_stale
