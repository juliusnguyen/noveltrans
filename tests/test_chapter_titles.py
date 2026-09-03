"""Feature 076 — the three guards that keep a model's own words out of a chapter title.

Every fixture here is a real string from the reporting library, not an invented one. That
matters twice over: the thresholds in `chapter_titles` were derived from that data, and the
false positives are the whole reason the design is shaped the way it is.
"""

from __future__ import annotations

import pytest

from noveltrans.chapter_titles import (
    is_implausible_title,
    looks_like_refusal,
    numeric_title,
    repaired_title,
)

# The ten refusals actually stored in novel-9d42408b, abbreviated only where the tail adds
# nothing. Two of them (111, 121) are the ones a marker list would not have anticipated.
REFUSALS = [
    "Xin lỗi, bạn chưa gửi kèm nội dung chương 90 để mình dịch. Bạn vui lòng dán văn bản "
    "tiếng Trung cần dịch, mình sẽ dịch sang tiếng Việt.",
    "Xin lỗi, bạn chưa cung cấp nội dung chương 127 để dịch. Vui lòng dán văn bản tiếng "
    "Trung cần dịch, tôi sẽ dịch sang tiếng Việt theo phong cách văn học chuyên nghiệp.",
    "Bạn vui lòng gửi nội dung chương 111 (văn bản tiếng Trung) để mình dịch giúp bạn.",
    "Không có nội dung văn bản nào được cung cấp ngoài tiêu đề chương. Vui lòng gửi phần "
    "văn bản cần dịch.",
]

# Correct translations that CONTAIN an apology, because the source contains 對不起. A
# detector keyed on "Xin lỗi" would destroy these, which is why phrasing is the secondary
# layer and never the primary one.
APOLOGY_TITLES = [
    ("第268章 對不起通訊器先生", "Chương 268: Xin lỗi nhé máy liên lạc tiên sinh"),
    ("第四十二章 ：對不起，表姐", "Chương 42: Xin lỗi, biểu tỷ"),
]


class TestNumericTitle:
    @pytest.mark.parametrize(
        "source,expected",
        [
            ("第127章", "Chương 127"),
            ("第90章", "Chương 90"),
            ("第1章", "Chương 1"),
            ("第 127 章", "Chương 127"),  # spaces around the number
            ("第001章", "Chương 1"),  # zero-padded, as novel-476d3ebf numbers
            ("  第127章  ", "Chương 127"),
            ("第127章：", "Chương 127"),  # trailing punctuation only
        ],
    )
    def test_a_bare_number_is_built_locally(self, source, expected):
        assert numeric_title(source) == expected

    @pytest.mark.parametrize(
        "source",
        [
            "第1章 重生",  # real title text — must reach the model
            "第001章 撿個破盆",
            "第1 章 穿書畜生",  # space before 章, novel-e5d29222
            "第1章 第 1 章 師尊，您過得可好啊",  # doubled numbering, novel-5313f5fe
            "第一章 ：獨孤九劍",  # Chinese numeral WITH title text
            "第六章 ：首戰（求月票）",
            "作品相關",  # front matter, not a chapter at all
            "Chương 1: Đội xe Đệ Nhất thiết luật",  # already Vietnamese
            "",
        ],
    )
    def test_anything_carrying_real_text_goes_to_the_model(self, source):
        assert numeric_title(source) == ""

    def test_it_spells_the_heading_word_per_language(self):
        assert numeric_title("第127章", "en") == "Chapter 127"

    def test_an_unknown_language_defers_to_the_model(self):
        # Inventing a heading word for a language we have no spelling for would be worse
        # than translating it.
        assert numeric_title("第127章", "ja") == ""


class TestIsImplausibleTitle:
    @pytest.mark.parametrize("refusal", REFUSALS)
    def test_every_real_refusal_is_caught(self, refusal):
        assert is_implausible_title("第127章", refusal)

    @pytest.mark.parametrize("source,output", APOLOGY_TITLES)
    def test_a_correct_translation_containing_an_apology_is_not_flagged(self, source, output):
        assert not is_implausible_title(source, output)

    @pytest.mark.parametrize(
        "source,output",
        [
            ("第127章", "Chương 127"),
            ("第1章 重生", "Chương 1: Trùng sinh"),
            # The longest legitimate title in the library, 107 chars — long in absolute
            # terms but a low ratio, so length alone must not condemn it.
            (
                "第607章 第一次當巡邏隊隊長，打得贏就打，打不贏就跑，這是我的原則問題",
                "Chương 607: Lần thứ nhất đương Đội trưởng đội tuần tra, đánh thắng được liền "
                "đánh, đánh không lại thì chạy, đây là vấn đề nguyên tắc của ta",
            ),
        ],
    )
    def test_legitimate_titles_pass(self, source, output):
        assert not is_implausible_title(source, output)

    def test_a_newline_is_enough_on_its_own(self):
        # The other damaged shape: the model returned the title AND the body prose. The
        # ratio here is only 5.5, under the threshold — the newline is what catches it.
        source = "第54章 忠僕剖心，皇兄的算盤珠子崩臉上！【謝禮物加更】"
        assert is_implausible_title(source, "Chương 54: Trung bộc phơi gan trải lòng\nHắn " * 3)

    def test_it_sits_outside_the_measured_ratio(self):
        # p99.9 of 4323 real titles is 5.91; the threshold is 6. Either side of it.
        source = "x" * 20
        assert not is_implausible_title(source, "y" * 119)  # 5.95x, still plausible
        assert is_implausible_title(source, "y" * 121)  # 6.05x

    def test_a_short_output_is_never_condemned_by_ratio_alone(self):
        # 15x, but 45 characters cannot hide a refusal — the shortest one observed is 97.
        assert not is_implausible_title("第1章", "Chương 1: Một cái tên khá dài nhưng thật")

    @pytest.mark.parametrize("source,output", [("", "bất kỳ"), ("第1章", ""), ("", "")])
    def test_an_empty_side_is_not_a_verdict(self, source, output):
        assert not is_implausible_title(source, output)


class TestLooksLikeRefusal:
    @pytest.mark.parametrize("refusal", REFUSALS)
    def test_every_real_refusal_is_caught(self, refusal):
        assert looks_like_refusal(refusal)

    @pytest.mark.parametrize("source,output", APOLOGY_TITLES)
    def test_an_apology_alone_is_not_a_refusal(self, source, output):
        assert not looks_like_refusal(output)

    def test_ordinary_prose_deep_in_a_chapter_is_not_a_refusal(self):
        # Only the opening is scanned: a long enough body will eventually contain a
        # sentence about handing something over, and that is narration, not a refusal.
        body = "Hắn bước vào phòng. " * 40 + "Nàng gửi nội dung bức thư cho hắn."
        assert not looks_like_refusal(body)

    def test_it_needs_both_halves(self):
        # A verb of giving with nothing being asked for, and vice versa.
        assert not looks_like_refusal("Hắn cung cấp một thanh kiếm cho sư đệ.")
        assert not looks_like_refusal("Nội dung bức thư khiến nàng sững sờ.")

    def test_an_empty_string_is_not_a_refusal(self):
        assert not looks_like_refusal("")


class TestRepairedTitle:
    def test_a_numeric_source_is_rebuilt_locally(self):
        assert repaired_title("第127章", REFUSALS[1]) == "Chương 127"

    def test_a_title_plus_body_leak_keeps_its_first_line(self):
        stored = "Chương 196: Ôn Thục Nghi bùng nổ\nTrước cửa thư viện Khung Thương, " * 3
        assert repaired_title("第196章 暴走的溫淑儀（求月票）", stored) == (
            "Chương 196: Ôn Thục Nghi bùng nổ"
        )

    def test_it_declines_when_the_first_line_is_itself_the_refusal(self):
        # Nothing recoverable and no number to rebuild from — say so rather than guess.
        assert repaired_title("第268章 對不起通訊器先生", REFUSALS[1]) == ""

    def test_it_honours_the_target_language(self):
        assert repaired_title("第127章", REFUSALS[1], "en") == "Chapter 127"
