"""Assert a built wheel can actually start.

The test suite imports `merge` through pytest's `pythonpath = ["src"]`, which resolves
it from the source tree and never from the built artifact. Full coverage therefore says
nothing about whether an installed copy runs: a wheel shipping the module as
`src/merge.py` went undetected for two releases and broke `calendar-merge` entirely.

Run against a wheel directory:

    python tools/assert_wheel_layout.py dist
"""

import configparser
import pathlib
import sys
import zipfile


class LayoutError(Exception):
    """The wheel would not import the module its entry point names."""


def _find_wheel(directory: pathlib.Path) -> pathlib.Path:
    wheels = sorted(directory.glob("*.whl"))
    if not wheels:
        raise LayoutError(f"no wheel found in {directory}")
    if len(wheels) > 1:
        # Stale artifacts would make the check ambiguous about what it verified.
        raise LayoutError(f"expected one wheel in {directory}, found: {[w.name for w in wheels]}")
    return wheels[0]


def _read_entry_points(archive: zipfile.ZipFile) -> configparser.ConfigParser:
    # Matched against the dist-info path rather than any name ending in
    # entry_points.txt, so a stray file cannot be read as metadata.
    candidates = [n for n in archive.namelist() if n.endswith(".dist-info/entry_points.txt")]
    if not candidates:
        raise LayoutError("wheel declares no entry points; the console script would not be installed")
    parsed = configparser.ConfigParser()
    parsed.read_string(archive.read(candidates[0]).decode())
    return parsed


def assert_wheel_layout(directory: pathlib.Path) -> list[str]:
    """Return the console scripts verified, or raise LayoutError."""
    wheel = _find_wheel(directory)
    # Closed before any LayoutError is raised; everything below needs only the names
    # and the parsed metadata, not the open archive.
    with zipfile.ZipFile(wheel) as archive:
        names = archive.namelist()
        entry_points = _read_entry_points(archive)

    if not entry_points.has_section("console_scripts"):
        raise LayoutError("wheel has entry points but no console_scripts section")

    scripts = dict(entry_points["console_scripts"])
    # An empty section would otherwise leave the loop below with nothing to do, and a
    # check that passes while verifying nothing is worse than no check at all.
    if not scripts:
        raise LayoutError("console_scripts section is empty; nothing to verify")

    verified: list[str] = []
    for script, target in scripts.items():
        module = target.split(":")[0].strip()
        # Derived from the entry point rather than hardcoded, so renaming the script
        # without moving the module fails as readily as the reverse.
        expected = module.replace(".", "/") + ".py"
        package_form = module.replace(".", "/") + "/__init__.py"
        if expected not in names and package_form not in names:
            raise LayoutError(
                f"console script {script!r} imports {module!r}, but neither {expected} "
                f"nor {package_form} is in the wheel root. Contents: {sorted(names)}"
            )
        verified.append(f"{script} -> {module}")
    return verified


def main(argv: list[str]) -> int:
    directory = pathlib.Path(argv[1] if len(argv) > 1 else "dist")
    try:
        for entry in assert_wheel_layout(directory):
            print(f"ok: {entry}")
    except LayoutError as err:
        print(f"wheel layout check failed: {err}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
