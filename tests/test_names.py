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
