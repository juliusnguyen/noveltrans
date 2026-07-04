VENV := .venv
PY   := $(VENV)/bin/python

.PHONY: run setup test lint clean

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

## Xoá venv và cache
clean:
	rm -rf $(VENV) .pytest_cache .ruff_cache src/*.egg-info
