r"""Resolve pinned tool versions from `uv.lock`, and keep the copies honest.

A tool's version is declared in more places than Dependabot can see: it updates
`pyproject.toml` and `uv.lock`, but not literals in `ci.yml` nor a `rev` in
`.pre-commit-config.yaml`. Ruff drifted that way silently on the 0.15.10 to 0.16.3
bump, CI linting with the previous release while local runs used the new one -- the
exact drift the pin was introduced to prevent.

The same hole was open for **mypy**, and wider: the hook pinned 1.20.1 while the
lockfile resolved 2.3.1, a major version apart. A pre-commit run and CI could disagree
about whether the code type-checks at all, in either direction -- a local pass that
fails CI, or a real type error accepted locally.

`uv.lock` is the single source of truth. CI reads a version from here rather than
repeating it, and every pre-commit `rev` is asserted against it because a `rev` cannot
be derived at run time. Uses only the standard library -- `tomllib`, so Python 3.11 or
newer -- so the lint job needs neither the private dependency nor a resolved
environment.

    python tools/pinned_versions.py ruff     # print the locked version
    python tools/pinned_versions.py --check  # assert every pinned rev matches
"""

import argparse
import pathlib
import re
import sys
import tomllib

LOCKFILE = pathlib.Path("uv.lock")
PRE_COMMIT = pathlib.Path(".pre-commit-config.yaml")

# One entry per tool whose pre-commit rev must track the lockfile. Keyed by the package
# name in uv.lock; the value is the pre-commit repository that pins it.
PINNED_TOOLS = {
    "ruff": "ruff-pre-commit",
    "mypy": "mirrors-mypy",
}

# The result is interpolated into a shell command in CI, and uv.lock is a checked-in
# file a fork pull request can edit. Anything outside this shape -- a quote, a
# semicolon, whitespace, a newline -- is rejected rather than passed along.
# re.ASCII is load-bearing: \d matches Unicode digits by default, which would widen
# this beyond ASCII for a value that reaches a shell. With the flag it is exactly
# [0-9]. Do not drop it when simplifying.
SAFE_VERSION = re.compile(r"\A\d[\dA-Za-z.+\-]*\Z", re.ASCII)


class VersionError(Exception):
    """A pinned version is missing, inconsistent, or not a version at all."""


def _rev_pattern(repository: str) -> re.Pattern[str]:
    r"""Locate the rev on one pre-commit repository only.

    Anchored to the repository name so an unrelated hook's rev is never mistaken for
    it. Captures the whole token rather than a restricted character class: validation
    belongs to SAFE_VERSION alone, or a malformed rev is silently truncated to its
    valid prefix and reported as drift instead of as malformed. Each part matches a
    disjoint character set, so any input has exactly one match path and there is no
    backtracking -- `\s*\n\s*` would be ambiguous, since \s includes the newline.
    """
    return re.compile(rf"{re.escape(repository)}[^\n]*\n[ \t]*rev:[ \t]*v?(?P<version>\S+)")


def _validated(version: str, source: pathlib.Path) -> str:
    """Return the version, or raise if it is not shaped like one."""
    if not SAFE_VERSION.match(version):
        raise VersionError(f"{source} declares {version!r}, which is not a valid version string")
    return version


def locked_version(package: str, lockfile: pathlib.Path = LOCKFILE) -> str:
    """Return the version of `package` resolved in the lockfile."""
    if not lockfile.is_file():
        raise VersionError(f"{lockfile} not found")
    for entry in tomllib.loads(lockfile.read_text()).get("package", []):
        if entry.get("name") == package:
            version = str(entry.get("version") or "")
            if not version:
                raise VersionError(f"{package} is in {lockfile} with no version")
            return _validated(version, lockfile)
    raise VersionError(f"{package} is not in {lockfile}; is it still a dev dependency?")


def pre_commit_version(repository: str, config: pathlib.Path = PRE_COMMIT) -> str:
    """Return the version pinned by one pre-commit repository."""
    if not config.is_file():
        raise VersionError(f"{config} not found")
    match = _rev_pattern(repository).search(config.read_text())
    if match is None:
        raise VersionError(f"no {repository} rev found in {config}")
    return _validated(match.group("version"), config)


def check(lockfile: pathlib.Path = LOCKFILE, config: pathlib.Path = PRE_COMMIT) -> dict[str, str]:
    """Return every locked version, or raise when a pre-commit rev disagrees.

    Every tool is checked before raising, so one bump does not hide another: a
    Dependabot PR moving both ruff and mypy should report both, not whichever the
    dictionary happened to order first.
    """
    versions: dict[str, str] = {}
    drifted: list[str] = []
    for package, repository in PINNED_TOOLS.items():
        locked = locked_version(package, lockfile)
        versions[package] = locked
        pinned = pre_commit_version(repository, config)
        if locked != pinned:
            drifted.append(f"{package}: {lockfile} has {locked}, {config} pins {pinned} (bump the rev to v{locked})")
    if drifted:
        raise VersionError(
            "version drift between the lockfile and the pre-commit hooks. Dependabot "
            "updates the lockfile but not a pre-commit rev - " + "; ".join(drifted)
        )
    return versions


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Resolve pinned tool versions from uv.lock.")
    parser.add_argument("package", nargs="?", help="print this package's locked version")
    parser.add_argument("--check", action="store_true", help="assert every pre-commit rev matches the lockfile")
    args = parser.parse_args(argv[1:])
    if not args.package and not args.check:
        parser.error("give a package name, --check, or both")
    try:
        if args.check:
            check()
        if args.package:
            print(locked_version(args.package))
    except VersionError as err:
        print(f"pinned version check failed: {err}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
