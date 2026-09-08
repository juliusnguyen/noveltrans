from noveltrans.translators.names import (
    apply_glossary,
    build_glossary,
    extract_names,
    to_hanviet,
)


class TestToHanviet:
    def test_basic_names(self):
        assert to_hanviet("傅清詞") == "Phó Thanh Từ"
        assert to_hanviet("江妤") == "Giang Dư"
        assert to_hanviet("林城安") == "Lâm Thành An"

    def test_surname_override(self):
        # 沈 reads "trầm" in general text but "Thẩm" as a surname
        assert to_hanviet("沈月") == "Thẩm Nguyệt"
        assert to_hanviet("沈月", as_name=False) == "Trầm Nguyệt"

    def test_unknown_char_returns_none(self):
        assert to_hanviet("傅") is None


class TestExtractNames:
    def test_finds_recurring_names(self):
        # the char after the name varies, as in real prose
        corpus = "江妤看着他。江妤走了。江妤說。江妤笑了。江妤哭了。江妤回家。"
        names = extract_names(corpus, min_count=5)
        assert "江妤" in names
        assert all(not n.startswith("江妤") or n == "江妤" for n in names)

    def test_longer_name_wins_when_third_char_is_stable(self):
        corpus = "林城安笑了。林城安走了。林城安說。林城安來。林城安去。"
        names = extract_names(corpus, min_count=5)
        assert "林城安" in names
        assert "林城" not in names

    def test_verb_after_name_not_absorbed(self):
        # 看 follows the name only 2/6 times -> not part of the name
        corpus = "江妤看他。江妤看她。江妤走。江妤說。江妤來。江妤去。"
        names = extract_names(corpus, min_count=5)
        assert "江妤" in names
        assert "江妤看" not in names

    def test_infrequent_names_skipped(self):
        names = extract_names("江妤來了。江妤走了。", min_count=5)
        assert names == {}

    def test_double_surname(self):
        corpus = "歐陽雪來了。歐陽雪走了。歐陽雪說。歐陽雪笑。歐陽雪哭。"
        assert "歐陽雪" in extract_names(corpus, min_count=5)


class TestGlossary:
    def test_build_and_apply(self):
        corpus = (
            "傅清詞看着江妤。傅清詞走了，江妤說。傅清詞笑，江妤哭。"
            "傅清詞回頭，江妤點頭。傅清詞離開。江妤微笑。傅清詞坐。江妤站。"
        )
        glossary = build_glossary(corpus, min_count=5)
        assert glossary["傅清詞"] == "Phó Thanh Từ"
        assert glossary["江妤"] == "Giang Dư"

        replaced = apply_glossary("傅清詞牽着江妤的手。", glossary)
        assert replaced == "Phó Thanh Từ牽着Giang Dư的手。"

    def test_common_words_not_treated_as_names(self):
        # 安全/高興/謝謝 start with surname chars but are ordinary vocabulary
        corpus = "安全第一。很安全。不安全。要安全。真安全。都安全。" \
                 "高興地說。很高興。不高興。真高興。太高興。也高興。"
        glossary = build_glossary(corpus, min_count=5)
        assert "安全" not in glossary
        assert "高興" not in glossary

    def test_measure_word_phrases_rejected(self):
        corpus = "一張臉。那張臉。這張臉。一張臉。那張臉。半張臉。"
        assert "張臉" not in build_glossary(corpus, min_count=5)

    def test_longest_name_replaced_first(self):
        glossary = {"林城": "Lâm Thành", "林城安": "Lâm Thành An"}
        assert apply_glossary("林城安在林城。", glossary) == "Lâm Thành An在Lâm Thành。"


class TestFamousNamesAreNotDroppedAsVocabulary:
    """Feature 086 — the dictionary gate was discarding names for being well known.

    `_is_common_word` asks jieba's frequency dictionary, and a REAL historical person is a
    dictionary entry: 尹志平 scores 367 and was rejected as ordinary vocabulary, while the
    purely fictional 郭靖 (score 0) passed. The better known the character, the more likely
    they were dropped — and in the reporting novel that was the protagonist, 14847 times.
    """

    def _corpus(self, name: str, times: int = 40) -> str:
        # varied context on both sides, so the preceder/follower gates do not fire
        return "".join(f"「{name}說道，這是第{i}次了。」\n那天晚上，{name}走了。\n" for i in range(times))

    def test_a_famous_three_character_name_is_kept(self):
        from noveltrans.translators.names import extract_names

        assert "尹志平" in extract_names(self._corpus("尹志平"))

    def test_a_fictional_name_still_works(self):
        from noveltrans.translators.names import extract_names

        assert "郭靖" in extract_names(self._corpus("郭靖"))

    def test_ordinary_two_character_vocabulary_is_still_rejected(self):
        """The gate still does its job where it was aimed: 安全 ("safety"), 高興 ("happy")
        and 任何 ("any") all begin with a surname and none of them is a person."""
        from noveltrans.translators.names import extract_names

        for word in ("安全", "高興", "任何"):
            assert word not in extract_names(self._corpus(word)), word

    def test_jiebas_own_person_tag_is_not_trusted(self):
        """Measured and rejected as the fix: jieba tags 武功 ("martial arts"), 熊貓
        ("panda") and 謝謝 ("thank you") as person names, so believing it would substitute
        invented names over ordinary words throughout a novel."""
        from noveltrans.translators.names import extract_names

        for word in ("武功", "熊貓", "謝謝"):
            assert word not in extract_names(self._corpus(word)), word


class TestSurnameReadings:
    def test_the_surname_reading_wins_over_the_ordinary_one(self):
        from noveltrans.translators.names import to_hanviet

        # Reported: the protagonist came out as "Duẫn Chí Bình"; 尹 as a surname is Doãn.
        assert to_hanviet("尹志平") == "Doãn Chí Bình"
        assert to_hanviet("任我行") == "Nhậm Ngã Hành"
        # 李 is written Lý in Vietnamese; the table's "lí" was being corrected by the
        # engines anyway (measured 3881 uses of "Lý Mạc Sầu" against 19 of "Lí").
        assert to_hanviet("李莫愁") == "Lý Mạc Sầu"

    def test_it_applies_only_to_the_first_character(self):
        from noveltrans.translators.names import to_hanviet

        # 任 inside a word keeps its ordinary reading — the override is positional.
        assert to_hanviet("任何", as_name=False) == "Nhâm Hà"
