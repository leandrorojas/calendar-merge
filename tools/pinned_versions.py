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

# Repositories that deliberately have no lockfile counterpart, with the reason. Every
# repo in the config must appear here or in PINNED_TOOLS, so a hook added later cannot
# sit unguarded and unnoticed -- a test asserts exactly that.
UNGUARDED_REPOS = {
    "pre-commit-hooks": "not a Python package; nothing in uv.lock to track",
}

# The result is interpolated into a shell command in CI, and uv.lock is a checked-in
# file a fork pull request can edit. Anything outside this shape -- a quote, a
# semicolon, whitespace, a newline -- is rejected rather than passed along.
# re.ASCII is load-bearing: \d matches Unicode digits by default, which would widen
# this beyond ASCII for a value that reaches a shell. With the flag it is exactly
# [0-9]. Do not drop it when simplifying.
SAFE_VERSION = re.compile(r"\A\d[\dA-Za-z.+\-]*\Z", re.ASCII)

# A `- repo:` line, capturing the trailing path segment. Anchored on the key so a
# comment mentioning a repository cannot be read as one, and on `/` plus end-of-token
# so `ruff-pre-commit-nightly` is not mistaken for `ruff-pre-commit`.
REPO_LINE = re.compile(r"^[ \t]*-[ \t]*repo:[ \t]*(?P<url>\S+)[ \t]*$")
REV_LINE = re.compile(r"^[ \t]*rev:[ \t]*v?(?P<version>\S+)[ \t]*$")
HOOK_ID_LINE = re.compile(r"^[ \t]*-[ \t]*id:[ \t]*(?P<id>\S+)[ \t]*$")


class VersionError(Exception):
    """A pinned version is missing, inconsistent, or not a version at all."""


def _repo_blocks(config: pathlib.Path) -> dict[str, list[list[str]]]:
    """Split the pre-commit config into the line-lists of each repository's blocks.

    Parsed by block rather than matched with a single pattern: a regex spanning from a
    repository name to the next `rev:` reads whatever lies between them, so a comment,
    a reordered `hooks:` key, or a similarly named repository silently changes which
    version is checked.

    A repository may appear more than once -- pre-commit runs every block, so listing
    one twice at different revs runs both. Keyed to a *list* of blocks rather than one,
    because keeping only the last let an older rev run unchecked while the guard
    validated the newer one and passed.
    """
    if not config.is_file():
        raise VersionError(f"{config} not found")
    blocks: dict[str, list[list[str]]] = {}
    current: list[str] | None = None
    for line in config.read_text().splitlines():
        match = REPO_LINE.match(line)
        if match:
            repository = match.group("url").rstrip("/").rsplit("/", 1)[-1]
            current = []
            blocks.setdefault(repository, []).append(current)
            continue
        if current is not None:
            current.append(line)
    return blocks


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
    """Return the version pinned by one pre-commit repository.

    Also requires the block to declare at least one hook. A repository pinned at the
    right version whose `hooks:` list is empty, or whose only `- id:` is commented out,
    runs nothing -- so agreeing with the lockfile would prove nothing.
    """
    blocks = _repo_blocks(config)
    if repository not in blocks:
        raise VersionError(f"no {repository} repo found in {config}")
    versions: list[str] = []
    for lines in blocks[repository]:
        found = [match for match in (REV_LINE.match(line) for line in lines) if match]
        if not found:
            raise VersionError(f"{repository} in {config} has no rev")
        if not any(HOOK_ID_LINE.match(line) for line in lines):
            raise VersionError(f"{repository} in {config} pins a rev but declares no hook, so nothing runs")
        versions.append(_validated(found[0].group("version"), config))
    # Every block runs, so every block must agree. Returning just one would let an
    # older duplicate keep running while the guard reported success.
    if len(set(versions)) > 1:
        raise VersionError(f"{repository} in {config} is pinned at more than one rev: {sorted(set(versions))}")
    return versions[0]


def check(lockfile: pathlib.Path = LOCKFILE, config: pathlib.Path = PRE_COMMIT) -> dict[str, str]:
    """Return every locked version, or raise when a pre-commit rev disagrees.

    Every tool is evaluated before raising -- including ones whose own lookup fails --
    so a bump moving two does not hide one behind the other, and a missing repo does
    not discard drift already found.

    An empty table is itself an error: a guard that checks nothing must not report
    success, which is the failure mode that made the wheel-layout check meaningless
    before it was hardened.
    """
    if not PINNED_TOOLS:
        raise VersionError("PINNED_TOOLS is empty, so this check would verify nothing")
    versions: dict[str, str] = {}
    problems: list[str] = []
    for package, repository in PINNED_TOOLS.items():
        try:
            locked = locked_version(package, lockfile)
            versions[package] = locked
            pinned = pre_commit_version(repository, config)
        except VersionError as err:
            problems.append(f"{package}: {err}")
            continue
        if locked != pinned:
            problems.append(f"{package}: {lockfile} has {locked}, {config} pins {pinned} (bump the rev to v{locked})")
    if problems:
        raise VersionError(
            "version drift between the lockfile and the pre-commit hooks. Dependabot "
            "updates the lockfile but not a pre-commit rev - " + "; ".join(problems)
        )
    return versions


def unguarded_repositories(config: pathlib.Path = PRE_COMMIT) -> set[str]:
    """Repositories in the config that neither PINNED_TOOLS nor UNGUARDED_REPOS covers.

    Exists so a hook added later cannot drift unnoticed: the table is only a guarantee
    if something checks the table itself is complete.
    """
    known = set(PINNED_TOOLS.values()) | set(UNGUARDED_REPOS)
    return {repo for repo in _repo_blocks(config) if repo not in known}


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
