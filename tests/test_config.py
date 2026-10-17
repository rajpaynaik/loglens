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


def test_project_root_is_the_nearest_config_inside_the_workspace(tmp_path):
    from logsite.config import project_root

    (tmp_path / "services" / "pay" / "app").mkdir(parents=True)
    (tmp_path / "services" / "pay" / "logsite.toml").write_text("")
    (tmp_path / "services" / "web").mkdir()
    (tmp_path / "services" / "web" / "pyproject.toml").write_text("[project]\nname = 'web'\n")
    (tmp_path / "services" / "api").mkdir()
    (tmp_path / "services" / "api" / "pyproject.toml").write_text("[tool.logsite]\n")

    pay = tmp_path / "services" / "pay"
    assert project_root(pay / "app" / "x.py", tmp_path) == pay
    assert project_root(tmp_path / "services" / "api" / "x.py", tmp_path) == pay.parent / "api"
    # a pyproject.toml without [tool.logsite] isn't a logsite project
    assert project_root(tmp_path / "services" / "web" / "x.py", tmp_path) == tmp_path
    assert project_root(tmp_path / "top.py", tmp_path) == tmp_path
    # config above the workspace is ignored: the workspace root wins
    assert project_root(pay / "app" / "x.py", pay / "app") == pay / "app"
