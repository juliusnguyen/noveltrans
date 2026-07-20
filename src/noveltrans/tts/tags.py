"""Build the prompt for, and parse the output of, LLM-generated YouTube tags.

Tags are semantic (genre inference, title variants with/without diacritics) so they are
produced by an LLM engine — the same CLI Agent / Claude / LM Studio engines used to
translate (via their `complete()` method). These helpers are pure (no network): a tab
worker calls `build_tags_prompt`, sends it to the chosen engine, then runs the reply
through `parse_tags`. `parse_tags` enforces YouTube's 500-character tag budget the way
YouTube counts it — a tag containing a space is billed with +2 for the quotes it needs —
so the kept list pastes in under the limit.
"""

from __future__ import annotations

import re

# YouTube caps the *combined* tag text at 500 characters; a multi-word tag is quoted, which
# costs 2 extra characters toward that budget (why the reference thumbnail reads "405/500").
YOUTUBE_TAG_CHAR_LIMIT = 500

_LEADING_BULLET = re.compile(r"^\s*(?:[-*•]|\d+[.)])\s*")


def build_tags_prompt(
    *,
    vn_title: str,
    original_title: str = "",
    author: str = "",
    vn_description: str = "",
) -> str:
    """A Vietnamese instruction asking the LLM for a comma-separated YouTube tag list.

    Requests the Vietnamese title plus variants WITH and WITHOUT diacritics, `truyện chữ` /
    `tiểu thuyết` variants, genre/theme tags, and format tags (`truyện audio`, `truyện dịch`,
    `review truyện`, `tóm tắt truyện`, …). Instructs the model to output ONLY the list.
    """
    vn_title = (vn_title or "").strip()
    context = [f"Tên truyện (tiếng Việt): {vn_title}"]
    if original_title.strip():
        context.append(f"Tên gốc: {original_title.strip()}")
    if author.strip():
        context.append(f"Tác giả: {author.strip()}")
    if vn_description.strip():
        # keep the prompt compact — a short excerpt is enough to infer genre
        context.append(f"Mô tả: {vn_description.strip()[:400]}")
    context_block = "\n".join(context)
    return (
        "Bạn là chuyên gia SEO cho kênh YouTube truyện audio tiếng Việt. "
        "Hãy tạo danh sách TỪ KHOÁ (tags) YouTube cho video đọc truyện dưới đây.\n\n"
        f"{context_block}\n\n"
        "Yêu cầu danh sách tags:\n"
        "- Khoảng 18–25 tag, bằng tiếng Việt.\n"
        "- Bao gồm tên truyện tiếng Việt VÀ biến thể KHÔNG DẤU của tên truyện "
        "(ví dụ: 'người hầu phản diện' và 'nguoi hau phan dien').\n"
        "- Bao gồm biến thể như 'truyện chữ …', 'tiểu thuyết …'.\n"
        "- Bao gồm tag thể loại/chủ đề (ví dụ: tiên hiệp, huyền huyễn, ngôn tình, "
        "hệ thống, xuyên sách, phản diện…).\n"
        "- Bao gồm tag định dạng: truyện audio, truyện dịch, truyện hay, review truyện, "
        "tóm tắt truyện.\n"
        "- Tổng độ dài tất cả tag KHÔNG vượt quá 500 ký tự.\n\n"
        "CHỈ trả về danh sách tag, phân tách bằng dấu phẩy, trên MỘT dòng. "
        "Không đánh số, không giải thích, không thêm chữ nào khác."
    )


def build_thumbnail_image_prompt(
    *,
    vn_title: str,
    original_title: str = "",
    vn_description: str = "",
    tagline: str = "",
) -> str:
    """A Vietnamese instruction asking the LLM to write an AI image-generation prompt.

    The reply is an English prompt (image models understand English best) describing a
    cinematic 16:9 scene fitting the novel — main character(s), setting, mood, art style —
    ready to paste into Midjourney / Stable Diffusion / DALL·E to make the thumbnail's base
    image. It must carry NO text/watermark (styled text is overlaid later by the renderer).
    """
    vn_title = (vn_title or "").strip()
    context = [f"Tên truyện (tiếng Việt): {vn_title}"]
    if original_title.strip():
        context.append(f"Tên gốc: {original_title.strip()}")
    if tagline.strip():
        context.append(f"Tagline: {tagline.strip()}")
    if vn_description.strip():
        context.append(f"Mô tả: {vn_description.strip()[:600]}")
    context_block = "\n".join(context)
    return (
        "Bạn là chuyên gia viết prompt cho AI tạo ảnh (Midjourney / Stable Diffusion / "
        "DALL·E). Hãy viết MỘT prompt để tạo ẢNH BÌA (thumbnail) cho video đọc truyện "
        "audio dưới đây.\n\n"
        f"{context_block}\n\n"
        "Yêu cầu:\n"
        "- Viết prompt bằng TIẾNG ANH (mô hình tạo ảnh hiểu tiếng Anh tốt nhất).\n"
        "- Mô tả một cảnh điện ảnh khớp nội dung truyện: nhân vật chính, bối cảnh, cảm xúc, "
        "ánh sáng.\n"
        "- Phong cách phù hợp truyện tiên hiệp/huyền huyễn/cổ trang Trung Quốc: digital "
        "painting điện ảnh, chi tiết cao, ánh sáng đẹp, tỉ lệ 16:9, độ phân giải cao.\n"
        "- TUYỆT ĐỐI không có chữ, watermark hay logo trong ảnh (chữ sẽ được thêm sau).\n\n"
        "CHỈ trả về nội dung prompt (một đoạn), không giải thích, không thêm chú thích."
    )


def _tag_cost(tag: str) -> int:
    """How many characters `tag` consumes of YouTube's 500-char budget."""
    return len(tag) + (2 if " " in tag else 0)


def _clean_tag(raw: str) -> str:
    """Normalise one candidate tag: strip bullets/quotes/hashes and collapse whitespace."""
    tag = _LEADING_BULLET.sub("", raw.strip())
    tag = tag.strip().strip('"').strip("'").lstrip("#").strip()
    return re.sub(r"\s+", " ", tag)


def parse_tags(raw: str, *, max_total_chars: int = YOUTUBE_TAG_CHAR_LIMIT) -> list[str]:
    """Parse an LLM reply into a clean, de-duplicated, budget-capped tag list.

    Splits on commas and newlines, normalises each tag, drops empties, de-duplicates
    case-insensitively (first occurrence wins, order preserved), then keeps tags in order
    until the running YouTube cost would exceed `max_total_chars`.
    """
    pieces = re.split(r"[,\n]+", raw or "")
    seen: set[str] = set()
    kept: list[str] = []
    total = 0
    for piece in pieces:
        tag = _clean_tag(piece)
        if not tag:
            continue
        key = tag.casefold()
        if key in seen:
            continue
        cost = _tag_cost(tag)
        if cost > max_total_chars:
            continue  # a single tag longer than the whole budget — skip it
        if total + cost > max_total_chars:
            break
        seen.add(key)
        kept.append(tag)
        total += cost
    return kept


def format_tags(tags: list[str]) -> str:
    """Join tags for storage / the `.tags.txt` file / the editable field."""
    return ", ".join(tags)
