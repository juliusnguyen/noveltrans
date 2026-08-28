"""Feature 069 — the source-site advertising filter. Pure: no engine, no model, no I/O.

The reported line is `Muốn xem thêm nhiều chương đặc sắc, xin truy cập sto9🍀.com`. It is
in the Chinese source, so the translator renders it faithfully and it lands mid-novel.

Two properties are being defended here and they pull in opposite directions, so the test
file is weighted the way the code is: a handful of tests confirm an ad line IS dropped, and
a larger class confirms ordinary prose is NOT. A missed ad is one visible junk line the
user can find-and-replace; an over-deletion is silent data loss noticed chapters later.
"""

from __future__ import annotations

import pytest

from noveltrans.translators.ads import drop_site_ads

REPORTED = "Muốn xem thêm nhiều chương đặc sắc, xin truy cập sto9🍀.com"

STORY = (
    "Diệp Vân mỉm cười: “Ngươi nghĩ ta sợ sao?” Hắn bước tới, ánh mắt lạnh lẽo.\n\n"
    "Năm 1234, tại thành Lạc Dương, một trận chiến kinh thiên động địa đã nổ ra!"
)


def _dropped(line: str) -> bool:
    """True when `line`, sitting between two real paragraphs, is filtered out."""
    return drop_site_ads(f"Trước.\n\n{line}\n\nSau.") == "Trước.\n\nSau."


class TestTheReportedLine:
    """The exact shape the user reported, and the variants the site can trivially switch to."""

    def test_the_reported_line_is_gone(self):
        assert _dropped(REPORTED)

    def test_the_same_line_without_the_emoji(self):
        assert _dropped("Muốn xem thêm nhiều chương đặc sắc, xin truy cập sto9.com")

    @pytest.mark.parametrize("glyph", ["🍀", "🌸", "★", "⭐", "✿", "✦", "❀"])
    def test_a_decorative_glyph_inserted_beside_the_dot(self, glyph):
        """The reported shape: a real dot, with something ornamental wedged in next to it."""
        assert _dropped(f"Muốn xem thêm chương mới nhất, truy cập sto9{glyph}.com")

    @pytest.mark.parametrize("glyph", ["·", "•", "。", "．", "｡", "﹒", "・"])
    def test_a_lookalike_standing_in_for_the_dot(self, glyph):
        """The other shape: no real dot at all, just something that reads as one."""
        assert _dropped(f"Muốn xem thêm chương mới nhất, truy cập sto9{glyph}com")

    def test_a_lookalike_next_to_a_real_dot_is_a_known_gap(self):
        """`sto9。.com` — both shapes at once — is NOT caught, deliberately.

        Catching it means collapsing runs of dots in the detection view, and that would
        make `"Anh ấy nói...com đã nguội rồi"` fold to `noi.com` and be deleted as an ad.
        Eating a line of dialogue is far worse than missing a shape no site actually uses;
        the two real obfuscations above are both covered.
        """
        assert not _dropped("Muốn xem thêm chương mới nhất, truy cập sto9。.com")

    def test_fullwidth_characters(self):
        assert _dropped("Truy cập ｓｔｏ９．ｃｏｍ để đọc tiếp")

    def test_letters_spaced_out(self):
        assert _dropped("Truy cập s t o 9 . c o m để đọc chương mới nhất")

    def test_the_untranslated_chinese_original(self):
        """If the model leaves the watermark in Chinese, it is still a watermark."""
        assert _dropped("想看更多精彩章節，請訪問 sto9🍀.com")

    @pytest.mark.parametrize(
        "line",
        [
            "Đọc tiếp tại trang web sto9.com nhé",
            "Chương mới nhất cập nhật nhanh nhất tại sto9.com",
            "Ghé thăm sto9.com để đọc miễn phí",
            "Địa chỉ mới của chúng tôi: sto9🍀.com",
        ],
    )
    def test_reworded_variants(self, line):
        """The Vietnamese wording is model output and varies run to run, so the filter
        cannot key on a literal phrase."""
        assert _dropped(line)


class TestBareDomainLines:
    @pytest.mark.parametrize(
        "line",
        [
            "sto9.com",
            "www.sto9.com",
            "【sto9.com】",
            "— sto9🍀.com —",
            "https://sto9.com/book/13908/index.html",
        ],
    )
    def test_a_line_that_is_only_a_domain_is_dropped(self, line):
        assert _dropped(line)


class TestLegitimateContentSurvives:
    """**The anti-regression class.** Every line here must come through untouched."""

    @pytest.mark.parametrize(
        "line",
        [
            # a domain in ordinary narration — no promo cue, no obfuscation
            "Hắn mở trình duyệt, gõ vào địa chỉ example.com rồi nhấn Enter, chờ trang tải xong.",
            "Hắn gõ vào ô tìm kiếm: google.com, rồi ngẩng đầu nhìn nàng, ánh mắt dò xét.",
            "Cô ấy gửi cho tôi đường link https://example.com/anh-dep rồi cười.",
            # the strongest promo cue in the list, with no domain anywhere
            "Truy cập vào hệ thống, hắn thấy một dòng chữ đỏ.",
            "Cậu vào trang web nào vậy?",
            "Trang web của công ty bị hack, dữ liệu biến mất.",
            # a missing space after a full stop must not fold into a domain
            "Rồi.Mẹ nói với hắn rằng chuyện đã xong.",
            "Cô bước tới bên cửa sổ. Có lẽ mọi thứ đã kết thúc.",
            # a site named without a TLD is not a domain
            "Hắn lên Weibo tìm kiếm, rồi chuyển sang diễn đàn Tấn Giang.",
            "Chương 5: Trận chiến tại Lạc Dương",
        ],
    )
    def test_it_survives(self, line):
        assert not _dropped(line)

    def test_a_long_paragraph_is_never_dropped(self):
        """Over the length cap, a real paragraph survives whatever else it contains."""
        long_line = "A" * 210 + " truy cập sto9.com"
        assert not _dropped(long_line)

    def test_ordinary_prose_is_returned_byte_identical(self):
        assert drop_site_ads(STORY) == STORY

    def test_is_idempotent(self):
        once = drop_site_ads(f"A.\n\n{REPORTED}\n\nB.")
        assert drop_site_ads(once) == once


class TestParagraphStructure:
    """Removal must leave no trace — no blank gap, and no paragraph split in two."""

    def test_an_ad_paragraph_leaves_exactly_one_break(self):
        out = drop_site_ads(f"A.\n\n{REPORTED}\n\nB.")
        assert out == "A.\n\nB."
        assert "\n\n\n" not in out

    def test_an_ad_line_inside_a_paragraph_does_not_split_it(self):
        out = drop_site_ads(f"L1\n{REPORTED}\nL2")
        assert out == "L1\nL2"
        assert "\n\n" not in out, "the paragraph was split in two"

    def test_a_leading_ad_leaves_no_blank_start(self):
        assert drop_site_ads(f"{REPORTED}\n\nA.") == "A."

    def test_a_trailing_ad_leaves_no_blank_end(self):
        assert drop_site_ads(f"A.\n\n{REPORTED}") == "A."

    def test_an_ad_joined_onto_a_real_sentence_is_left_alone(self):
        """A documented limitation, pinned so it stays a decision rather than a bug.

        The unit is a whole line; cutting a clause out of one is where over-deletion
        lives. The source CMS puts the watermark on its own `<br>`, which the scraper
        turns into its own paragraph, so this shape should be rare — and the prompt rule
        is the mitigation for when it is not.
        """
        joined = f"Hắn nói xong rồi quay đi. {REPORTED}"
        assert drop_site_ads(joined) == joined


class TestSafetyValves:
    def test_a_text_that_is_only_an_ad_is_returned_unchanged(self):
        """`translate_chapter` routes the novel title and description through here too.
        A blanked field would be far worse than a surviving ad line."""
        assert drop_site_ads(REPORTED) == REPORTED

    @pytest.mark.parametrize("text", ["", "   ", "\n\n"])
    def test_empty_input_is_returned_as_given(self, text):
        assert drop_site_ads(text) == text
