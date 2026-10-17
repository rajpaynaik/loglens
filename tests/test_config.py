from logsite.config import load_config


def test_reads_logsite_toml(tmp_path):
    (tmp_path / "logsite.toml").write_text("window_hours = 6\n")
    assert load_config(tmp_path).window_hours == 6


def test_still_reads_the_old_loglens_names(tmp_path):
    (tmp_path / "loglens.toml").write_text("window_hours = 7\n")
    assert load_config(tmp_path).window_hours == 7

    other = tmp_path / "other"
    other.mkdir()
    (other / "pyproject.toml").write_text("[tool.loglens]\nwindow_hours = 8\n")
    assert load_config(other).window_hours == 8


def test_new_names_win_over_old(tmp_path):
    (tmp_path / "logsite.toml").write_text("window_hours = 1\n")
    (tmp_path / "loglens.toml").write_text("window_hours = 2\n")
    assert load_config(tmp_path).window_hours == 1

    other = tmp_path / "other"
    other.mkdir()
    (other / "pyproject.toml").write_text(
        "[tool.logsite]\nwindow_hours = 3\n[tool.loglens]\nwindow_hours = 4\n"
    )
    assert load_config(other).window_hours == 3
