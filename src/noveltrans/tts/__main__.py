"""Debug CLI: render a preview of every preset voice to listen and compare.

    python -m noveltrans.tts                     # all voices -> ~/NovelTrans/voice-previews
    python -m noveltrans.tts --voice "Thái Sơn"  # just one (repeatable)
    python -m noveltrans.tts --text "Câu tuỳ ý, {voice} sẽ được thay bằng tên giọng."
"""

from __future__ import annotations

import argparse
from pathlib import Path

from noveltrans.tts import get_tts_engine
from noveltrans.tts.convert import convert_to_mp3, ffmpeg_available

DEFAULT_TEXT = (
    "Xin chào, tôi là {voice}. "
    "Giang Dư nhìn vết bầm tím trên cổ tay, trong đầu hiện ra cảnh hôm nay. "
    "Phó Thanh Từ khẽ cau mày, giọng nói trầm xuống: đừng để bị thương thêm lần nữa."
)
DEFAULT_OUT = Path.home() / "NovelTrans" / "voice-previews"


def main() -> None:
    parser = argparse.ArgumentParser(description="Render mỗi giọng VieNeu-TTS một file nghe thử.")
    parser.add_argument("--text", default=DEFAULT_TEXT, help="câu thử ({voice} = tên giọng)")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT, help="thư mục output")
    parser.add_argument(
        "--voice", action="append", default=None, help="chỉ render giọng này (lặp lại được)"
    )
    parser.add_argument("--wav", action="store_true", help="giữ WAV, không chuyển MP3")
    args = parser.parse_args()

    engine = get_tts_engine("vieneu")
    print("Đang nạp model VieNeu (lần đầu tải ~330 MB)…")
    engine.load()
    voices = engine.list_voices()
    if args.voice:
        wanted = {v.lower() for v in args.voice}
        voices = [(label, vid) for label, vid in voices if vid.lower() in wanted]
        if not voices:
            raise SystemExit(f"Không có giọng nào khớp {args.voice}.")

    to_mp3 = ffmpeg_available() and not args.wav
    args.out.mkdir(parents=True, exist_ok=True)
    for label, voice_id in voices:
        engine.voice = voice_id
        out_path = args.out / f"{voice_id}.wav"
        seconds = engine.synthesize_chapter("", args.text.format(voice=voice_id), out_path)
        if to_mp3:
            out_path = convert_to_mp3(out_path)
        print(f"  ✓ {label:<32} -> {out_path.name} ({seconds:.1f}s)")
    print(f"\nXong — mở thư mục: {args.out}")


if __name__ == "__main__":
    main()
