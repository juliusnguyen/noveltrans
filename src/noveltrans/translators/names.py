"""Hán-Việt name glossary for the Google engine.

Google Translate renders Chinese proper names as pinyin ("Fu Qingci"), while
Vietnamese novel-reading convention is Sino-Vietnamese ("Phó Thanh Từ").
This module detects recurring character names in the *original* Chinese text
(surname + 1-2 chars) and replaces them with their Hán-Việt reading BEFORE
the text is sent to Google, so names come back consistent and correct.

Character readings come from data/hanviet.json (Unihan kVietnamese merged
with the translation-community phienam list, which wins on conflicts).
"""

from __future__ import annotations

import json
import re
from functools import lru_cache
from importlib import resources

# Common Chinese surnames, simplified + traditional forms.
_SINGLE_SURNAMES = (
    "王李张張刘劉陈陳杨楊黄赵趙吴吳周徐孙孫马馬朱胡郭何高林罗羅郑鄭梁谢謝宋唐许許韩韓冯馮"
    "邓鄧曹彭曾肖田董袁潘于蒋蔣蔡余杜叶葉程苏蘇魏吕呂丁任沈姚卢盧姜崔钟鍾谭譚陆陸汪范範金"
    "石廖贾賈夏韦韋傅方白邹鄒孟熊秦邱江尹薛闫段雷侯龙龍史陶黎贺賀顾顧毛郝龚龔邵万萬钱錢严"
    "嚴覃武戴莫孔向汤湯温溫裴席蓝藍季骆駱耿聂聶焦岳纪紀童柏牧遲迟楚顏颜苗凌霍虞柳祁俞雲云"
    "容景寧宁蒙桑仇甘慕连連庄莊温司艾洛池滕安路谷婁娄封燕楼樓宫宮池"
)
_DOUBLE_SURNAMES = (
    "欧阳", "歐陽", "司马", "司馬", "上官", "慕容", "南宫", "南宮", "东方", "東方",
    "独孤", "獨孤", "司徒", "诸葛", "諸葛", "皇甫", "尉迟", "尉遲", "令狐", "轩辕", "軒轅",
)

# Readings that differ when the character is used as a SURNAME.
_SURNAME_READING_OVERRIDES = {
    "沈": "thẩm",
    "单": "thiện",
    "單": "thiện",
    "解": "giải",
    "曾": "tăng",
    "查": "tra",
}

# Function/grammar characters that never appear inside given names. A candidate
# whose given part contains one of these is a phrase, not a person.
_FUNCTION_CHARS = set(
    "的了不是在有就都也還还沒没著着來来去這这那個个們们把被向於于與与和跟為为"
    "會会能可要當当從从對对上下裏里外前後后中間间到出入起過过再又或而且但如果"
    "因所以什麼么怎樣样已經经曾即使得地你我他她它咱您誰谁很太最更些每次回件事"
    "時时候讓让給给收拾接站坐走跑飛飞開开關关說说話话道問问答叫喊聽听見见知覺觉"
    "自己"
)

# Kinship/title words: 傅先生 is "Mr. Fu", not a given name.
_TITLE_WORDS = {
    "先生", "小姐", "太太", "夫人", "女士", "少爺", "少爷", "老爺", "老爷",
    "爺爺", "爷爷", "奶奶", "媽媽", "妈妈", "爸爸", "哥哥", "姐姐", "弟弟",
    "妹妹", "叔叔", "阿姨", "老師", "老师", "醫生", "医生", "總裁", "总裁",
    "董事", "秘書", "秘书", "老板", "老闆", "師傅", "师傅", "大人", "公子",
    "姑娘", "嫂子", "大哥", "大姐", "老頭", "老头", "老太", "氏",
    # single-char kinship/title suffixes: 傅家 "nhà họ Phó", 江母 "mẹ Giang"…
    "家", "母", "父", "總", "总", "姨", "叔", "嫂", "哥", "姐", "少",
}

# Characters that precede measure/duration phrases (一張臉, 那段時間…).
_DETERMINER_CHARS = set("一二三四五六七八九十兩两幾几那這这半每整成數数")


@lru_cache(maxsize=1)
def _word_freq() -> dict:
    """jieba's built-in word-frequency dictionary (simplified Chinese)."""
    import jieba

    jieba.initialize()
    return jieba.dt.FREQ


@lru_cache(maxsize=1)
def _to_simplified():
    from opencc import OpenCC

    return OpenCC("t2s").convert


def _is_common_word(word: str, min_freq: int) -> bool:
    """True if `word` is an ordinary Chinese vocabulary word, not a name."""
    return (_word_freq().get(_to_simplified()(word)) or 0) >= min_freq

_CJK = r"一-鿿"
_NAME_RE = re.compile(
    "(" + "|".join(_DOUBLE_SURNAMES) + f"|[{re.escape(_SINGLE_SURNAMES)}])([{_CJK}]{{1,2}})"
)


@lru_cache(maxsize=1)
def hanviet_table() -> dict[str, str]:
    data = resources.files("noveltrans.translators").joinpath("data/hanviet.json")
    return json.loads(data.read_text(encoding="utf-8"))


def to_hanviet(name: str, as_name: bool = True) -> str | None:
    """'傅清詞' -> 'Phó Thanh Từ'; None if any character has no known reading."""
    table = hanviet_table()
    syllables: list[str] = []
    for i, char in enumerate(name):
        reading = None
        if as_name and i == 0:
            reading = _SURNAME_READING_OVERRIDES.get(char)
        reading = reading or table.get(char)
        if not reading:
            return None
        syllables.append(reading.capitalize())
    return " ".join(syllables)


def extract_names(corpus: str, min_count: int = 5) -> dict[str, int]:
    """Find recurring surname+given-name strings in Chinese text.

    Returns {name: count}. For every recurring surname+char pair we accept the
    longer 2-char given name (林城安 over 林城) only when the third character
    follows the pair in >=80% of its occurrences — otherwise that character is
    just whatever happens to follow the name (a verb, a particle, …).
    """
    pair_counts: dict[str, int] = {}
    ext_counts: dict[str, int] = {}
    preceding: dict[str, dict[str, int]] = {}
    for match in _NAME_RE.finditer(corpus):
        surname, given = match.group(1), match.group(2)
        base = surname + given[0]
        pair_counts[base] = pair_counts.get(base, 0) + 1
        prev = corpus[match.start() - 1] if match.start() > 0 else ""
        preceding.setdefault(base, {})
        preceding[base][prev] = preceding[base].get(prev, 0) + 1
        if len(given) == 2:
            full = surname + given
            ext_counts[full] = ext_counts.get(full, 0) + 1

    names: dict[str, int] = {}
    for base, base_count in pair_counts.items():
        if base_count < min_count:
            continue
        # a real name is preceded by varied context; a phrase like 一段時間 or
        # 立馬會意 is dominated by one specific CJK character before it
        prev_counts = preceding.get(base, {})
        if prev_counts:
            top_prev, top_count = max(prev_counts.items(), key=lambda kv: kv[1])
            if re.match(f"[{_CJK}]", top_prev or "") and top_count > 0.5 * base_count:
                continue
            # measure-word patterns: 一張臉, 那段時間 — determiners dominate
            determiner_count = sum(
                c for prev, c in prev_counts.items() if prev in _DETERMINER_CHARS
            )
            if determiner_count > 0.6 * base_count:
                continue

        extensions = {n: c for n, c in ext_counts.items() if n.startswith(base)}
        best, best_count = (
            max(extensions.items(), key=lambda kv: kv[1]) if extensions else (None, 0)
        )
        candidate, count = (
            (best, best_count)
            if best is not None and best_count >= 0.8 * base_count
            else (base, base_count)
        )

        given_part = candidate[2:] if candidate[:2] in _DOUBLE_SURNAMES else candidate[1:]
        if any(char in _FUNCTION_CHARS for char in given_part):
            continue
        if given_part in _TITLE_WORDS:
            continue
        # ordinary vocabulary whose first char happens to be a surname
        # (安全, 高興, 許多…) or whose "given name" is a common noun (孫媳婦)
        if _is_common_word(candidate, min_freq=50):
            continue
        if len(given_part) >= 2 and _is_common_word(given_part, min_freq=200):
            continue
        # fragment of a longer fixed unit: 傅氏集(團), 溫文爾(雅) — a real name
        # is followed by varied text, a fragment always by the same character
        if _dominant_follower(corpus, candidate):
            continue
        names[candidate] = count
    return names


def _dominant_follower(corpus: str, candidate: str) -> bool:
    """True if one CJK character follows `candidate` in >=80% of occurrences."""
    followers: dict[str, int] = {}
    total = 0
    start = corpus.find(candidate)
    while start != -1:
        end = start + len(candidate)
        follower = corpus[end] if end < len(corpus) else ""
        followers[follower] = followers.get(follower, 0) + 1
        total += 1
        start = corpus.find(candidate, end)
    if not total:
        return False
    top_char, top_count = max(followers.items(), key=lambda kv: kv[1])
    return bool(re.match(f"[{_CJK}]", top_char or "")) and top_count >= 0.8 * total


def build_glossary(corpus: str, min_count: int = 5) -> dict[str, str]:
    """{chinese_name: 'Hán Việt'} for every convertible recurring name."""
    glossary: dict[str, str] = {}
    for name in extract_names(corpus, min_count):
        hanviet = to_hanviet(name)
        if hanviet:
            glossary[name] = hanviet
    return glossary


def apply_glossary(text: str, glossary: dict[str, str]) -> str:
    """Replace Chinese names with their Hán-Việt reading, longest names first."""
    for name in sorted(glossary, key=len, reverse=True):
        text = text.replace(name, glossary[name])
    return text
