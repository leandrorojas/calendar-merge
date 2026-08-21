"""Resolve the pinned Ruff version from `uv.lock`, and keep the copies honest.

The version is declared in more places than Dependabot can see: it updates
`pyproject.toml` and `uv.lock`, but not the literals in `ci.yml` nor the `rev` in
`.pre-commit-config.yaml`. A bump therefore left CI linting with the previous release
while local runs used the new one -- the exact drift the pin was introduced to prevent,
and it happened silently on the 0.15.10 to 0.16.3 bump.

`uv.lock` is the single source of truth. CI reads the version from here rather than
repeating it, and the pre-commit `rev` is asserted against it because a `rev` cannot be
derived at run time. Uses only the standard library, so the lint job still needs no
private dependency.

    python tools/ruff_version.py           # print the locked version
    python tools/ruff_version.py --check   # also assert the pre-commit rev matches
"""

import argparse
import pathlib
import re
import sys
import tomllib

LOCKFILE = pathlib.Path("uv.lock")
PRE_COMMIT = pathlib.Path(".pre-commit-config.yaml")
# Matches the rev on the ruff-pre-commit repo only, so an unrelated hook's rev is
# never mistaken for it.
PRE_COMMIT_REV = re.compile(r"ruff-pre-commit\s*\n\s*rev:\s*v?(?P<version>[0-9][0-9a-zA-Z.\-+]*)")


class VersionError(Exception):
    """The pinned Ruff version is missing or inconsistent."""


def locked_version(lockfile: pathlib.Path = LOCKFILE) -> str:
    """Return the Ruff version resolved in the lockfile."""
    if not lockfile.is_file():
        raise VersionError(f"{lockfile} not found")
    packages = tomllib.loads(lockfile.read_text()).get("package", [])
    for package in packages:
        if package.get("name") == "ruff":
            version = package.get("version")
            if not version:
                raise VersionError(f"ruff is in {lockfile} with no version")
            return str(version)
    raise VersionError(f"ruff is not in {lockfile}; is it still a dev dependency?")


def pre_commit_version(config: pathlib.Path = PRE_COMMIT) -> str:
    """Return the Ruff version pinned by the pre-commit hook."""
    if not config.is_file():
        raise VersionError(f"{config} not found")
    match = PRE_COMMIT_REV.search(config.read_text())
    if match is None:
        raise VersionError(f"no ruff-pre-commit rev found in {config}")
    return match.group("version")


def check(lockfile: pathlib.Path = LOCKFILE, config: pathlib.Path = PRE_COMMIT) -> str:
    """Return the locked version, or raise when the pre-commit rev disagrees."""
    locked = locked_version(lockfile)
    pinned = pre_commit_version(config)
    if locked != pinned:
        raise VersionError(
            f"ruff version drift: {lockfile} has {locked}, {config} pins {pinned}. "
            f"Dependabot updates the lockfile but not the pre-commit rev, so bump "
            f"{config} to v{locked}."
        )
    return locked


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="also assert the pre-commit rev matches")
    args = parser.parse_args(argv[1:])
    try:
        print(check() if args.check else locked_version())
    except VersionError as err:
        print(f"ruff version check failed: {err}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
