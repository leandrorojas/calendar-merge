"""Tests for the Ruff version resolver used by CI.

The version is declared in more places than Dependabot can see. This resolver makes
`uv.lock` the single source and asserts the pre-commit `rev` agrees, so the drift that
happened silently on the 0.15.10 to 0.16.3 bump fails the build instead.
"""

import pytest
from ruff_version import VersionError, check, locked_version, pre_commit_version

LOCK = """
[[package]]
name = "other"
version = "1.0.0"

[[package]]
name = "ruff"
version = "0.16.3"
"""

PRE_COMMIT = """
repos:
  - repo: https://github.com/astral-sh/ruff-pre-commit
    rev: v0.16.3
    hooks:
      - id: ruff
"""


def write(tmp_path, lock=LOCK, pre_commit=PRE_COMMIT):
    (tmp_path / "uv.lock").write_text(lock)
    (tmp_path / ".pre-commit-config.yaml").write_text(pre_commit)
    return tmp_path / "uv.lock", tmp_path / ".pre-commit-config.yaml"


class TestLockedVersion:
    def test_reads_the_resolved_version(self, tmp_path):
        lock, _ = write(tmp_path)

        assert locked_version(lock) == "0.16.3"

    def test_reports_a_missing_lockfile(self, tmp_path):
        with pytest.raises(VersionError, match="not found"):
            locked_version(tmp_path / "absent.lock")

    def test_reports_ruff_absent_from_the_lockfile(self, tmp_path):
        lock, _ = write(tmp_path, lock='[[package]]\nname = "other"\nversion = "1.0.0"\n')

        with pytest.raises(VersionError, match="not in"):
            locked_version(lock)

    def test_reports_an_entry_with_no_version(self, tmp_path):
        lock, _ = write(tmp_path, lock='[[package]]\nname = "ruff"\n')

        with pytest.raises(VersionError, match="no version"):
            locked_version(lock)


class TestVersionValidation:
    """The result reaches a shell in CI, and uv.lock is editable by a fork PR."""

    @pytest.mark.parametrize(
        "hostile",
        [
            '0.1"; echo INJECTED; #',
            "0.1 && curl evil",
            "0.1 | sh",
            "0.1$(id)",
            "0.1`id`",
            "0.1;id",
            "",
        ],
    )
    def test_rejects_anything_not_shaped_like_a_version(self, tmp_path, hostile):
        lock = tmp_path / "uv.lock"
        lock.write_text(f'[[package]]\nname = "ruff"\nversion = {hostile!r}\n')

        with pytest.raises(VersionError):
            locked_version(lock)

    @pytest.mark.parametrize("good", ["0.16.3", "2.9.0.post0", "1.0.0rc1", "0.16.3+local", "1.2.3-beta.1"])
    def test_accepts_real_version_shapes(self, tmp_path, good):
        lock = tmp_path / "uv.lock"
        lock.write_text(f'[[package]]\nname = "ruff"\nversion = "{good}"\n')

        assert locked_version(lock) == good

    def test_rejects_a_hostile_pre_commit_rev(self, tmp_path):
        config = tmp_path / ".pre-commit-config.yaml"
        config.write_text("repos:\n  - repo: https://github.com/astral-sh/ruff-pre-commit\n    rev: v0.1;id\n")

        with pytest.raises(VersionError, match="not a valid version"):
            pre_commit_version(config)


class TestPreCommitVersion:
    def test_reads_the_rev(self, tmp_path):
        _, config = write(tmp_path)

        assert pre_commit_version(config) == "0.16.3"

    def test_accepts_a_rev_without_the_v_prefix(self, tmp_path):
        _, config = write(tmp_path, pre_commit=PRE_COMMIT.replace("v0.16.3", "0.16.3"))

        assert pre_commit_version(config) == "0.16.3"

    def test_ignores_revs_belonging_to_other_hooks(self, tmp_path):
        """A rev is only read from the ruff-pre-commit repo."""
        config_text = """
repos:
  - repo: https://github.com/other/hook
    rev: v9.9.9
  - repo: https://github.com/astral-sh/ruff-pre-commit
    rev: v0.16.3
"""
        _, config = write(tmp_path, pre_commit=config_text)

        assert pre_commit_version(config) == "0.16.3"

    def test_reports_a_missing_config(self, tmp_path):
        with pytest.raises(VersionError, match="not found"):
            pre_commit_version(tmp_path / "absent.yaml")

    def test_reports_no_ruff_hook(self, tmp_path):
        _, config = write(tmp_path, pre_commit="repos:\n  - repo: https://github.com/other/hook\n    rev: v1.0.0\n")

        with pytest.raises(VersionError, match="no ruff-pre-commit rev"):
            pre_commit_version(config)


class TestCheck:
    def test_returns_the_version_when_both_agree(self, tmp_path):
        lock, config = write(tmp_path)

        assert check(lock, config) == "0.16.3"

    def test_detects_the_drift_it_exists_for(self, tmp_path):
        """Reproduces the 0.15.10 -> 0.16.3 bump that CI silently ignored."""
        lock, config = write(tmp_path, pre_commit=PRE_COMMIT.replace("v0.16.3", "v0.15.10"))

        with pytest.raises(VersionError, match=r"0\.15\.10"):
            check(lock, config)

    def test_the_message_names_the_fix(self, tmp_path):
        lock, config = write(tmp_path, pre_commit=PRE_COMMIT.replace("v0.16.3", "v0.15.10"))

        with pytest.raises(VersionError, match=r"bump .* to v0\.16\.3"):
            check(lock, config)


class TestRealRepository:
    def test_this_repository_is_consistent(self):
        """Guards the checked-in files, not a fixture."""
        assert check() == locked_version()
