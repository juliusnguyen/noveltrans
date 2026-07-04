from noveltrans.storage.state import AppState


class TestAppState:
    def test_fresh_state(self, tmp_path):
        state = AppState(tmp_path / ".noveltrans")
        assert state.last_project == ""
        assert state.recent == []
        assert state.valid_last_project() == ""

    def test_touch_and_reload(self, tmp_path):
        state_dir = tmp_path / ".noveltrans"
        proj = tmp_path / "lib" / "novel-abc"
        proj.mkdir(parents=True)
        (proj / "meta.json").write_text("{}")

        state = AppState(state_dir)
        state.touch_project(str(proj))

        reloaded = AppState(state_dir)
        assert reloaded.last_project == str(proj)
        assert reloaded.recent == [str(proj)]
        assert reloaded.valid_last_project() == str(proj)

    def test_recent_dedup_and_order(self, tmp_path):
        state = AppState(tmp_path / ".noveltrans")
        state.touch_project("/a")
        state.touch_project("/b")
        state.touch_project("/a")
        assert state.recent == ["/a", "/b"]
        assert state.last_project == "/a"

    def test_deleted_project_not_valid(self, tmp_path):
        state = AppState(tmp_path / ".noveltrans")
        state.touch_project(str(tmp_path / "gone"))
        assert state.valid_last_project() == ""

    def test_corrupt_state_file_ignored(self, tmp_path):
        state_dir = tmp_path / ".noveltrans"
        state_dir.mkdir()
        (state_dir / "state.json").write_text("{not json")
        state = AppState(state_dir)
        assert state.last_project == ""
