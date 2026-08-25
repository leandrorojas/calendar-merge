"""Tests for the Ruff version resolver used by CI.

The version is declared in more places than Dependabot can see. This resolver makes
`uv.lock` the single source and asserts the pre-commit `rev` agrees, so the drift that
happened silently on the 0.15.10 to 0.16.3 bump fails the build instead.
"""

import pytest
from pinned_versions import (
    PINNED_TOOLS,
    VersionError,
    check,
    locked_version,
    pre_commit_version,
    unguarded_repositories,
)

LOCK = """
[[package]]
name = "other"
version = "1.0.0"

[[package]]
name = "ruff"
version = "0.16.3"

[[package]]
name = "mypy"
version = "2.3.1"
"""

PRE_COMMIT = """
repos:
  - repo: https://github.com/astral-sh/ruff-pre-commit
    rev: v0.16.3
    hooks:
      - id: ruff-check
  - repo: https://github.com/pre-commit/mirrors-mypy
    rev: v2.3.1
    hooks:
      - id: mypy
"""


def write(tmp_path, lock=LOCK, pre_commit=PRE_COMMIT):
    (tmp_path / "uv.lock").write_text(lock)
    (tmp_path / ".pre-commit-config.yaml").write_text(pre_commit)
    return tmp_path / "uv.lock", tmp_path / ".pre-commit-config.yaml"


class TestLockedVersion:
    def test_reads_the_resolved_version(self, tmp_path):
        lock, _ = write(tmp_path)

        assert locked_version("ruff", lock) == "0.16.3"

    def test_reports_a_missing_lockfile(self, tmp_path):
        with pytest.raises(VersionError, match="not found"):
            locked_version("ruff", tmp_path / "absent.lock")

    def test_reports_ruff_absent_from_the_lockfile(self, tmp_path):
        lock, _ = write(tmp_path, lock='[[package]]\nname = "other"\nversion = "1.0.0"\n')

        with pytest.raises(VersionError, match="not in"):
            locked_version("ruff", lock)

    def test_reports_an_entry_with_no_version(self, tmp_path):
        lock, _ = write(tmp_path, lock='[[package]]\nname = "ruff"\n')

        with pytest.raises(VersionError, match="no version"):
            locked_version("ruff", lock)


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
            locked_version("ruff", lock)

    def test_rejects_non_ascii_digits(self, tmp_path):
        """re.ASCII keeps \\d equal to [0-9]; without it this would be accepted."""
        lock = tmp_path / "uv.lock"
        lock.write_text('[[package]]\nname = "ruff"\nversion = "\u0660.\u0661"\n')

        with pytest.raises(VersionError, match="not a valid version"):
            locked_version("ruff", lock)

    @pytest.mark.parametrize("good", ["0.16.3", "2.9.0.post0", "1.0.0rc1", "0.16.3+local", "1.2.3-beta.1"])
    def test_accepts_real_version_shapes(self, tmp_path, good):
        lock = tmp_path / "uv.lock"
        lock.write_text(f'[[package]]\nname = "ruff"\nversion = "{good}"\n')

        assert locked_version("ruff", lock) == good

    def test_rejects_a_hostile_pre_commit_rev(self, tmp_path):
        config = tmp_path / ".pre-commit-config.yaml"
        config.write_text(
            "repos:\n  - repo: https://github.com/astral-sh/ruff-pre-commit\n"
            "    rev: v0.1;id\n    hooks:\n      - id: ruff-check\n"
        )

        with pytest.raises(VersionError, match="not a valid version"):
            pre_commit_version("ruff-pre-commit", config)


class TestPreCommitVersion:
    def test_reads_the_rev(self, tmp_path):
        _, config = write(tmp_path)

        assert pre_commit_version("ruff-pre-commit", config) == "0.16.3"

    def test_accepts_a_rev_without_the_v_prefix(self, tmp_path):
        _, config = write(tmp_path, pre_commit=PRE_COMMIT.replace("v0.16.3", "0.16.3"))

        assert pre_commit_version("ruff-pre-commit", config) == "0.16.3"

    def test_ignores_revs_belonging_to_other_hooks(self, tmp_path):
        """A rev is only read from the ruff-pre-commit repo."""
        config_text = """
repos:
  - repo: https://github.com/other/hook
    rev: v9.9.9
    hooks:
      - id: other
  - repo: https://github.com/astral-sh/ruff-pre-commit
    rev: v0.16.3
    hooks:
      - id: ruff-check
"""
        _, config = write(tmp_path, pre_commit=config_text)

        assert pre_commit_version("ruff-pre-commit", config) == "0.16.3"

    def test_reports_a_missing_config(self, tmp_path):
        with pytest.raises(VersionError, match="not found"):
            pre_commit_version("ruff-pre-commit", tmp_path / "absent.yaml")

    def test_reports_no_ruff_repo(self, tmp_path):
        _, config = write(
            tmp_path,
            pre_commit="repos:\n  - repo: https://github.com/other/hook\n    rev: v1.0.0\n    hooks:\n      - id: x\n",
        )

        with pytest.raises(VersionError, match="no ruff-pre-commit repo"):
            pre_commit_version("ruff-pre-commit", config)


class TestCheck:
    def test_returns_every_locked_version_when_all_agree(self, tmp_path):
        lock, config = write(tmp_path)

        assert check(lock, config) == {"ruff": "0.16.3", "mypy": "2.3.1"}

    def test_detects_the_drift_it_exists_for(self, tmp_path):
        """Reproduces the 0.15.10 -> 0.16.3 bump that CI silently ignored."""
        lock, config = write(tmp_path, pre_commit=PRE_COMMIT.replace("v0.16.3", "v0.15.10"))

        with pytest.raises(VersionError, match=r"0\.15\.10"):
            check(lock, config)

    def test_detects_mypy_drift_too(self, tmp_path):
        """The same hole was open for mypy, a full major version apart."""
        lock, config = write(tmp_path, pre_commit=PRE_COMMIT.replace("v2.3.1", "v1.20.1"))

        with pytest.raises(VersionError, match=r"mypy.*1\.20\.1"):
            check(lock, config)

    def test_reports_every_drifted_tool_not_just_the_first(self, tmp_path):
        """A bump moving both must not hide one behind the other."""
        drifted = PRE_COMMIT.replace("v0.16.3", "v0.15.10").replace("v2.3.1", "v1.20.1")
        lock, config = write(tmp_path, pre_commit=drifted)

        with pytest.raises(VersionError) as excinfo:
            check(lock, config)

        assert "ruff" in str(excinfo.value)
        assert "mypy" in str(excinfo.value)

    def test_the_message_names_the_fix(self, tmp_path):
        lock, config = write(tmp_path, pre_commit=PRE_COMMIT.replace("v0.16.3", "v0.15.10"))

        with pytest.raises(VersionError, match=r"bump the rev to v0\.16\.3"):
            check(lock, config)

    def test_no_repository_is_left_unguarded(self):
        """The table is only a guarantee if something checks the table is complete.

        Asserting its literal contents only catches removals. A hook added later
        without an entry is exactly the drift this guard exists to prevent, and would
        otherwise leave every test and `--check` green.
        """
        assert unguarded_repositories() == set()

    def test_an_unlisted_repository_is_reported(self, tmp_path):
        config = tmp_path / ".pre-commit-config.yaml"
        config.write_text(
            PRE_COMMIT + "  - repo: https://github.com/someone/new-linter\n    rev: v1.0.0\n    hooks:\n      - id: x\n"
        )

        assert unguarded_repositories(config) == {"new-linter"}

    def test_an_empty_table_is_an_error(self, tmp_path, monkeypatch):
        """A guard that verifies nothing must not report success."""
        lock, config = write(tmp_path)
        monkeypatch.setattr("pinned_versions.PINNED_TOOLS", {})

        with pytest.raises(VersionError, match="would verify nothing"):
            check(lock, config)

    def test_a_rev_without_a_hook_is_rejected(self, tmp_path):
        """A repo whose hooks list is empty runs nothing, so agreeing proves nothing."""
        lock, config = write(tmp_path, pre_commit=PRE_COMMIT.replace("      - id: ruff-check\n", ""))

        with pytest.raises(VersionError, match="declares no hook"):
            check(lock, config)

    def test_a_duplicate_block_at_an_older_rev_is_caught(self, tmp_path):
        """pre-commit runs every block, so keeping only the last hid a running hook.

        An older `ruff-pre-commit` listed first still runs; validating only the newer
        block reported success while ruff 0.9.0 was what actually executed.
        """
        stale = (
            "repos:\n  - repo: https://github.com/astral-sh/ruff-pre-commit\n"
            "    rev: v0.9.0\n    hooks:\n      - id: ruff-check\n"
        )
        lock, config = write(tmp_path, pre_commit=stale + PRE_COMMIT.split("repos:\n", 1)[1])

        with pytest.raises(VersionError, match="more than one rev"):
            check(lock, config)

    def test_a_duplicate_block_at_the_same_rev_is_allowed(self, tmp_path):
        """Splitting hooks across two blocks at one rev is legitimate, not drift."""
        extra = (
            "repos:\n  - repo: https://github.com/astral-sh/ruff-pre-commit\n"
            "    rev: v0.16.3\n    hooks:\n      - id: ruff-format\n"
        )
        lock, config = write(tmp_path, pre_commit=extra + PRE_COMMIT.split("repos:\n", 1)[1])

        assert check(lock, config)["ruff"] == "0.16.3"

    def test_a_similarly_named_repository_is_not_mistaken(self, tmp_path):
        """`ruff-pre-commit-nightly` must not be read as `ruff-pre-commit`."""
        nightly = (
            "repos:\n  - repo: https://github.com/x/ruff-pre-commit-nightly\n"
            "    rev: v9.9.9\n    hooks:\n      - id: x\n"
        )
        lock, config = write(tmp_path, pre_commit=nightly + PRE_COMMIT.split("repos:\n", 1)[1])

        assert check(lock, config)["ruff"] == "0.16.3"

    def test_drift_is_reported_even_when_a_later_tool_is_missing(self, tmp_path):
        """A failed lookup must not discard drift already found."""
        only_ruff = (
            "repos:\n  - repo: https://github.com/astral-sh/ruff-pre-commit\n"
            "    rev: v0.15.10\n    hooks:\n      - id: ruff-check\n"
        )
        lock, config = write(tmp_path, pre_commit=only_ruff)

        with pytest.raises(VersionError) as excinfo:
            check(lock, config)

        assert "0.15.10" in str(excinfo.value)
        assert "mypy" in str(excinfo.value)


class TestRealRepository:
    def test_this_repository_is_consistent(self):
        """Guards the checked-in files, not a fixture."""
        assert check() == {tool: locked_version(tool) for tool in PINNED_TOOLS}
