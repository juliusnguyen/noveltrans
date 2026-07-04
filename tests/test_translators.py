import json
from unittest.mock import MagicMock, patch

import pytest
import responses

from noveltrans.errors import TranslateError
from noveltrans.translators import get_translator
from noveltrans.translators.base import Translator, split_paragraph_chunks


class TestChunking:
    def test_short_text_single_chunk(self):
        assert split_paragraph_chunks("hello\n\nworld", 100) == ["hello\n\nworld"]

    def test_splits_on_paragraph_boundary(self):
        text = "aaaa\n\nbbbb\n\ncccc"
        chunks = split_paragraph_chunks(text, 10)
        assert chunks == ["aaaa\n\nbbbb", "cccc"]
        # no paragraph is ever split across chunks
        for chunk in chunks:
            for para in chunk.split("\n\n"):
                assert para in text

    def test_oversized_paragraph_kept_whole(self):
        text = "x" * 50
        assert split_paragraph_chunks(text, 10) == [text]

    def test_empty(self):
        assert split_paragraph_chunks("", 10) == []

    def test_rejoin_preserves_order(self):
        paras = [f"para-{i}-" + "y" * 20 for i in range(10)]
        chunks = split_paragraph_chunks("\n\n".join(paras), 60)
        assert "\n\n".join(chunks) == "\n\n".join(paras)


class FakeTranslator(Translator):
    """Deterministic engine for testing base-class behavior."""

    name = "fake"
    max_chunk_chars = 30
    retry_delay = 0.0

    def __init__(self, fail_times: int = 0):
        self.calls: list[str] = []
        self._fail_times = fail_times

    def translate(self, text: str, source: str = "zh", target: str = "vi") -> str:
        if self._fail_times > 0:
            self._fail_times -= 1
            raise RuntimeError("transient")
        self.calls.append(text)
        return f"[{target}]{text}"


class LeftoverTranslator(Translator):
    """Engine whose first attempts leave untranslated CJK residue in the output."""

    name = "leftover"
    retry_delay = 0.0

    def __init__(self, dirty_outputs: list[str]):
        self.calls = 0
        self._dirty_outputs = list(dirty_outputs)

    def translate(self, text: str, source: str = "zh", target: str = "vi") -> str:
        self.calls += 1
        if self._dirty_outputs:
            return self._dirty_outputs.pop(0)
        return "Phó Thanh Từ nhíu mày."


class TestTranslateChapter:
    def test_translates_title_and_chunks(self):
        engine = FakeTranslator()
        title, content = engine.translate_chapter(
            "chapter-1", "aaaaaaaaaaaaaaaaaaaaaaaaa\n\nbbbbbbbbbbbbbbbbbbbbbbbbb", target="vi"
        )
        assert title == "[vi]chapter-1"
        assert content == "[vi]aaaaaaaaaaaaaaaaaaaaaaaaa\n\n[vi]bbbbbbbbbbbbbbbbbbbbbbbbb"
        assert len(engine.calls) == 3  # title + 2 chunks

    def test_retries_when_cjk_left_in_output(self):
        engine = LeftoverTranslator(["Phó Thanh Từ皺眉, quay đầu."])
        _, content = engine.translate_chapter("", "原文")
        assert content == "Phó Thanh Từ nhíu mày."
        assert engine.calls == 2  # dirty first attempt triggered one retry

    def test_keeps_cleanest_attempt_when_always_dirty(self):
        engine = LeftoverTranslator(["一二三 bẩn nhiều", "一 bẩn ít", "一二 bẩn vừa"])
        _, content = engine.translate_chapter("", "原文")
        assert content == "一 bẩn ít"  # fewest leftovers beats failing the chapter
        assert engine.calls == engine.max_retries

    def test_retries_then_succeeds(self):
        engine = FakeTranslator(fail_times=2)
        title, content = engine.translate_chapter("t", "body")
        assert content == "[vi]body"

    def test_retries_exhausted_raises(self):
        engine = FakeTranslator(fail_times=99)
        with pytest.raises(TranslateError, match="after 3 tries"):
            engine.translate_chapter("t", "body")


class TestGoogleFree:
    def test_translate_maps_langs(self):
        with patch("noveltrans.translators.google_free.GoogleTranslator") as MockGT:
            MockGT.return_value.translate.return_value = "xin chào"
            engine = get_translator("google", request_delay=0)
            result = engine.translate("你好", source="zh", target="vi")
            assert result == "xin chào"
            MockGT.assert_called_once_with(source="zh-CN", target="vi")

    def test_none_result_becomes_empty(self):
        with patch("noveltrans.translators.google_free.GoogleTranslator") as MockGT:
            MockGT.return_value.translate.return_value = None
            engine = get_translator("google", request_delay=0)
            assert engine.translate("你好") == ""


class TestClaude:
    def _response(self, text: str):
        block = MagicMock()
        block.type = "text"
        block.text = text
        response = MagicMock()
        response.content = [block]
        return response

    def test_requires_api_key(self):
        with pytest.raises(TranslateError, match="API key"):
            get_translator("claude", api_key="")

    def test_translate(self):
        with patch("noveltrans.translators.claude.anthropic.Anthropic") as MockClient:
            MockClient.return_value.messages.create.return_value = self._response("Chào em")
            engine = get_translator("claude", api_key="sk-test", model="test-model")
            assert engine.translate("你好", target="vi") == "Chào em"
            kwargs = MockClient.return_value.messages.create.call_args.kwargs
            assert kwargs["model"] == "test-model"
            assert "Vietnamese" in kwargs["system"]
            assert kwargs["messages"] == [{"role": "user", "content": "你好"}]

    def test_empty_response_raises(self):
        with patch("noveltrans.translators.claude.anthropic.Anthropic") as MockClient:
            MockClient.return_value.messages.create.return_value = self._response("")
            engine = get_translator("claude", api_key="sk-test")
            engine.max_retries = 1
            with pytest.raises(TranslateError):
                engine.translate("你好")


class TestCliAgent:
    def _result(self, stdout="", stderr="", returncode=0):
        result = MagicMock()
        result.stdout = stdout
        result.stderr = stderr
        result.returncode = returncode
        return result

    def test_translate_runs_command_with_prompt(self):
        with patch("noveltrans.translators.cli_agent.subprocess.run") as mock_run:
            mock_run.return_value = self._result(stdout="Phó Thanh Từ cười.\n")
            engine = get_translator("cli", cli_command="agy -p --model 'Gemini 3.1 Pro (Low)'")
            assert engine.translate("傅清辭笑了。", target="vi") == "Phó Thanh Từ cười."
            # neutral cwd so agent CLIs don't pick up a code repo's context
            import tempfile

            assert mock_run.call_args.kwargs["cwd"] == tempfile.gettempdir()
            args = mock_run.call_args.args[0]
            # agy gets a debug log for error detail; the flag must precede -p
            assert args[0] == "agy"
            assert args[1] == "--log-file"
            assert args[3:6] == ["-p", "--model", "Gemini 3.1 Pro (Low)"]
            assert "傅清辭笑了。" in args[-1]
            assert "Hán-Việt" in args[-1]

    def test_model_flag_inserted_before_p(self):
        # agy silently ignores flags placed after -p, so --model must follow the binary
        engine = get_translator("cli", cli_command="agy -p", model="Claude Sonnet 4.6 (Thinking)")
        assert engine.args == ["agy", "--model", "Claude Sonnet 4.6 (Thinking)", "-p"]

    def test_model_overrides_command_model(self):
        engine = get_translator("cli", cli_command="agy -p --model 'Old Model'", model="New Model")
        assert engine.args == ["agy", "--model", "New Model", "-p"]

    def test_no_model_keeps_command_untouched(self):
        engine = get_translator("cli", cli_command="agy -p --model 'Old Model'")
        assert engine.args == ["agy", "-p", "--model", "Old Model"]

    def test_non_agy_command_gets_no_log_flag(self):
        with patch("noveltrans.translators.cli_agent.subprocess.run") as mock_run:
            mock_run.return_value = self._result(stdout="Chào em\n")
            engine = get_translator("claude_cli", cli_command="claude -p")
            engine.translate("你好")
            args = mock_run.call_args.args[0]
            assert "--log-file" not in args

    def test_empty_command_raises(self):
        with pytest.raises(TranslateError, match="Chưa cấu hình"):
            get_translator("cli", cli_command="  ")

    def test_missing_binary_raises(self):
        with patch(
            "noveltrans.translators.cli_agent.subprocess.run",
            side_effect=FileNotFoundError(),
        ):
            engine = get_translator("cli", cli_command="no-such-cli -p")
            engine.max_retries = 1
            with pytest.raises(TranslateError, match="Không tìm thấy lệnh"):
                engine.translate("你好")

    def test_nonzero_exit_raises(self):
        with patch("noveltrans.translators.cli_agent.subprocess.run") as mock_run:
            mock_run.return_value = self._result(stderr="quota exceeded", returncode=1)
            engine = get_translator("cli", cli_command="agy -p")
            engine.max_retries = 1
            engine.retry_delay = 0.0
            with pytest.raises(TranslateError, match="quota exceeded"):
                engine.translate("你好")

    def test_empty_output_raises(self):
        with patch("noveltrans.translators.cli_agent.subprocess.run") as mock_run:
            mock_run.return_value = self._result(stdout="  \n")
            engine = get_translator("cli", cli_command="agy -p")
            engine.max_retries = 1
            engine.retry_delay = 0.0
            with pytest.raises(TranslateError, match="không trả về"):
                engine.translate("你好")

    def test_empty_output_reports_quota_error_from_agy_log(self):
        quota_msg = (
            "RESOURCE_EXHAUSTED (code 429): Individual quota reached. "
            "Please upgrade your subscription to increase your limits. "
            "Resets in 4h24m9s."
        )

        def fake_run(cmd, **kwargs):
            log_path = cmd[cmd.index("--log-file") + 1]
            with open(log_path, "w") as fh:
                fh.write("I0705 00:21:22.587079 83855 log_context.go:117] retrying\n")
                fh.write(f"E0705 00:21:26.054671 83855 log.go:398] {quota_msg}: {quota_msg}\n")
            return self._result(stdout="")

        with patch("noveltrans.translators.cli_agent.subprocess.run", side_effect=fake_run):
            engine = get_translator("cli", cli_command="agy -p")
            engine.max_retries = 1
            engine.retry_delay = 0.0
            with pytest.raises(TranslateError, match=r"quota.*reset sau 4h24m9s"):
                engine.translate("你好")

    def test_empty_output_reports_generic_error_from_agy_log(self):
        def fake_run(cmd, **kwargs):
            log_path = cmd[cmd.index("--log-file") + 1]
            with open(log_path, "w") as fh:
                fh.write("E0705 00:21:26.054671 83855 log.go:398] model unreachable: connection refused\n")
            return self._result(stdout="")

        with patch("noveltrans.translators.cli_agent.subprocess.run", side_effect=fake_run):
            engine = get_translator("cli", cli_command="agy -p")
            engine.max_retries = 1
            engine.retry_delay = 0.0
            with pytest.raises(TranslateError, match="connection refused"):
                engine.translate("你好")


class TestLmStudio:
    URL = "http://127.0.0.1:1234"

    def _chat_response(self, content):
        return {"choices": [{"message": {"content": content}}]}

    @responses.activate
    def test_translate_posts_chat_completion(self):
        responses.add(
            responses.POST,
            f"{self.URL}/v1/chat/completions",
            json=self._chat_response("Phó Thanh Từ cười."),
        )
        engine = get_translator("lmstudio", base_url=self.URL, model="qwen3-14b")
        assert engine.translate("傅清辭笑了。", target="vi") == "Phó Thanh Từ cười."
        payload = json.loads(responses.calls[0].request.body)
        assert payload["model"] == "qwen3-14b"
        assert payload["messages"][1]["content"] == "傅清辭笑了。"
        assert "Hán-Việt" in payload["messages"][0]["content"]

    @responses.activate
    def test_empty_model_resolves_from_server(self):
        responses.add(
            responses.GET,
            f"{self.URL}/v1/models",
            json={"data": [{"id": "hunyuan-mt-7b"}, {"id": "other"}]},
        )
        responses.add(
            responses.POST,
            f"{self.URL}/v1/chat/completions",
            json=self._chat_response("Chào"),
        )
        engine = get_translator("lmstudio", base_url=self.URL)
        assert engine.translate("你好") == "Chào"
        payload = json.loads(responses.calls[1].request.body)
        assert payload["model"] == "hunyuan-mt-7b"

    @responses.activate
    def test_no_loaded_model_raises(self):
        responses.add(responses.GET, f"{self.URL}/v1/models", json={"data": []})
        engine = get_translator("lmstudio", base_url=self.URL)
        engine.max_retries = 1
        with pytest.raises(TranslateError, match="không có model"):
            engine.translate("你好")

    @responses.activate
    def test_strips_think_block(self):
        responses.add(
            responses.POST,
            f"{self.URL}/v1/chat/completions",
            json=self._chat_response("<think>\nhmm, names…\n</think>\nChào em"),
        )
        engine = get_translator("lmstudio", base_url=self.URL, model="qwen3")
        assert engine.translate("你好") == "Chào em"

    @responses.activate
    def test_unreachable_server_raises(self):
        import requests as requests_lib

        responses.add(
            responses.POST,
            f"{self.URL}/v1/chat/completions",
            body=requests_lib.exceptions.ConnectionError(),
        )
        engine = get_translator("lmstudio", base_url=self.URL, model="m")
        engine.max_retries = 1
        with pytest.raises(TranslateError, match="Không kết nối được LM Studio"):
            engine.translate("你好")

    @responses.activate
    def test_http_error_raises(self):
        responses.add(
            responses.POST,
            f"{self.URL}/v1/chat/completions",
            status=404,
            body="model not found",
        )
        engine = get_translator("lmstudio", base_url=self.URL, model="m")
        engine.max_retries = 1
        with pytest.raises(TranslateError, match="HTTP 404"):
            engine.translate("你好")

    def test_default_url_and_trailing_slash(self):
        engine = get_translator("lmstudio", base_url="http://192.168.1.5:5678/")
        assert engine.base_url == "http://192.168.1.5:5678"
        assert get_translator("lmstudio").base_url == "http://127.0.0.1:1234"


class TestTranslateWorkerLabel:
    def test_engine_labels(self, tmp_path):
        from noveltrans.gui.workers import TranslateWorker

        cases = [
            (dict(engine_name="google"), "Google Translate"),
            (dict(engine_name="claude", model="claude-haiku-4-5"), "Claude API (claude-haiku-4-5)"),
            (dict(engine_name="cli", cli_command="agy -p"), "CLI (agy)"),
            (dict(engine_name="claude_cli", cli_command="claude -p"), "CLI (claude)"),
            (
                dict(engine_name="cli", cli_command="agy -p", model="Gemini 3.1 Pro (Low)"),
                "CLI (agy, Gemini 3.1 Pro (Low))",
            ),
            (dict(engine_name="lmstudio", model="qwen3-14b"), "LM Studio (qwen3-14b)"),
            (dict(engine_name="lmstudio"), "LM Studio"),
        ]
        for kwargs, expected in cases:
            worker = TranslateWorker(tmp_path, target_lang="vi", **kwargs)
            assert worker.engine_label() == expected


class TestRegistry:
    def test_unknown_translator(self):
        with pytest.raises(TranslateError, match="Unknown translator"):
            get_translator("bing")

    def test_claude_cli_uses_cli_command(self):
        engine = get_translator("claude_cli", cli_command="claude -p")
        assert engine.args == ["claude", "-p"]
