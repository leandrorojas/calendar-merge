"""Tests for the CI wheel-layout check.

The check exists because the rest of the suite cannot see packaging: `merge` is
imported through pytest's `pythonpath = ["src"]`, never from a built wheel. Its whole
value is therefore in failing correctly, which is what these pin -- a check that passes
while verifying nothing is worse than no check at all.
"""

import pathlib
import sys
import zipfile

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "tools"))

from assert_wheel_layout import LayoutError, assert_wheel_layout, main

WORKING = {
    "x-1.0.dist-info/entry_points.txt": "[console_scripts]\ncalendar-merge = merge:main\n",
    "merge.py": "",
}


def wheel_dir(tmp_path, files, name="x-1.0-py3-none-any.whl"):
    with zipfile.ZipFile(tmp_path / name, "w") as archive:
        for path, body in files.items():
            archive.writestr(path, body)
    return tmp_path


class TestAssertWheelLayout:
    def test_accepts_a_wheel_that_would_import(self, tmp_path):
        assert assert_wheel_layout(wheel_dir(tmp_path, WORKING)) == ["calendar-merge -> merge"]

    def test_rejects_a_module_that_is_not_at_the_root(self, tmp_path):
        """The v0.1.8 bug: include without sources leaves the module under src/."""
        files = dict(WORKING)
        del files["merge.py"]
        files["src/merge.py"] = ""

        with pytest.raises(LayoutError, match=r"neither merge\.py"):
            assert_wheel_layout(wheel_dir(tmp_path, files))

    def test_rejects_an_empty_console_scripts_section(self, tmp_path):
        """Otherwise the loop has nothing to do and the check passes vacuously."""
        files = {"x-1.0.dist-info/entry_points.txt": "[console_scripts]\n", "merge.py": ""}

        with pytest.raises(LayoutError, match="empty"):
            assert_wheel_layout(wheel_dir(tmp_path, files))

    def test_rejects_entry_points_without_console_scripts(self, tmp_path):
        files = {"x-1.0.dist-info/entry_points.txt": "[gui_scripts]\na = b:c\n", "merge.py": ""}

        with pytest.raises(LayoutError, match="no console_scripts"):
            assert_wheel_layout(wheel_dir(tmp_path, files))

    def test_rejects_a_wheel_with_no_entry_points(self, tmp_path):
        with pytest.raises(LayoutError, match="no entry points"):
            assert_wheel_layout(wheel_dir(tmp_path, {"x-1.0.dist-info/METADATA": "Name: x\n"}))

    def test_ignores_a_stray_entry_points_file(self, tmp_path):
        """Metadata is read from dist-info, not from any similarly named file."""
        files = {"data/entry_points.txt": "[console_scripts]\ncm = merge:main\n", "merge.py": ""}

        with pytest.raises(LayoutError, match="no entry points"):
            assert_wheel_layout(wheel_dir(tmp_path, files))

    def test_accepts_a_package_rather_than_a_module(self, tmp_path):
        """A future layout may ship merge/ as a package instead of merge.py."""
        files = {
            "x-1.0.dist-info/entry_points.txt": "[console_scripts]\ncalendar-merge = merge:main\n",
            "merge/__init__.py": "",
        }

        assert assert_wheel_layout(wheel_dir(tmp_path, files)) == ["calendar-merge -> merge"]

    def test_reports_when_there_is_no_wheel(self, tmp_path):
        with pytest.raises(LayoutError, match="no wheel found"):
            assert_wheel_layout(tmp_path)

    def test_refuses_to_guess_between_several_wheels(self, tmp_path):
        """Stale artifacts would make it ambiguous which wheel was verified."""
        wheel_dir(tmp_path, WORKING, name="a-1.0-py3-none-any.whl")
        wheel_dir(tmp_path, WORKING, name="b-2.0-py3-none-any.whl")

        with pytest.raises(LayoutError, match="expected one wheel"):
            assert_wheel_layout(tmp_path)


class TestMain:
    def test_returns_zero_and_reports_what_it_verified(self, tmp_path, capsys):
        assert main(["prog", str(wheel_dir(tmp_path, WORKING))]) == 0
        assert "ok: calendar-merge -> merge" in capsys.readouterr().out

    def test_returns_one_and_explains_the_failure(self, tmp_path, capsys):
        assert main(["prog", str(tmp_path)]) == 1
        assert "wheel layout check failed" in capsys.readouterr().err

    def test_defaults_to_the_dist_directory(self, tmp_path, monkeypatch, capsys):
        dist = tmp_path / "dist"
        dist.mkdir()
        wheel_dir(dist, WORKING)
        monkeypatch.chdir(tmp_path)

        assert main(["prog"]) == 0
        assert "ok:" in capsys.readouterr().out
