VENV := .venv
PY   := $(VENV)/bin/python

.PHONY: run setup test lint clean icon app dmg

## Mở ứng dụng (tự cài đặt lần đầu nếu chưa có venv)
run: $(VENV)
	$(VENV)/bin/noveltrans

## Tạo venv Python 3.12 + cài dependencies
setup $(VENV):
	uv venv --python 3.12 $(VENV)
	uv pip install -e ".[dev]"

## Chạy toàn bộ test offline
test:
	$(PY) -m pytest

## Test chạm site thật (kiểm tra site có đổi giao diện)
test-live:
	$(PY) -m pytest -m live

## Kiểm tra lint
lint:
	$(VENV)/bin/ruff check src tests

## Vẽ lại icon (packaging/NovelTrans.icns) từ PNG
icon:
	QT_QPA_PLATFORM=offscreen $(PY) packaging/make_icon.py packaging/NovelTrans.png
	rm -rf packaging/NovelTrans.iconset && mkdir packaging/NovelTrans.iconset
	for s in 16 32 128 256 512; do \
		sips -z $$s $$s packaging/NovelTrans.png --out packaging/NovelTrans.iconset/icon_$${s}x$${s}.png >/dev/null; \
		d=$$((s*2)); \
		sips -z $$d $$d packaging/NovelTrans.png --out packaging/NovelTrans.iconset/icon_$${s}x$${s}@2x.png >/dev/null; \
	done
	iconutil -c icns packaging/NovelTrans.iconset -o packaging/NovelTrans.icns
	rm -rf packaging/NovelTrans.iconset

## Đóng gói thành dist/NovelTrans.app (cần TTS: uv pip install -e ".[tts]")
app: $(VENV)
	uv pip install --python $(PY) pyinstaller
	cd packaging && ../$(VENV)/bin/pyinstaller --noconfirm --clean \
		--distpath ../dist --workpath ../build/pyinstaller NovelTrans.spec
	@echo "→ dist/NovelTrans.app"

## Đóng gói .app rồi tạo dist/NovelTrans.dmg để kéo vào Applications
dmg: app
	rm -f dist/NovelTrans.dmg
	hdiutil create -volname NovelTrans -srcfolder dist/NovelTrans.app \
		-ov -format UDZO dist/NovelTrans.dmg
	@echo "→ dist/NovelTrans.dmg"

## Xoá venv và cache
clean:
	rm -rf $(VENV) .pytest_cache .ruff_cache src/*.egg-info build dist
