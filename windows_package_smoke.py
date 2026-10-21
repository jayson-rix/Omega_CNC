"""Smoke-test a Windows PyInstaller distribution from a clean directory."""

from __future__ import annotations

import ast
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


HERE = Path(__file__).resolve().parent
DEFAULT_DIST = HERE / "dist" / "Omega_CNC_Processor"


def _catalog_machine_count() -> int:
    tree = ast.parse((HERE / "main.py").read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.AnnAssign):
            if isinstance(node.target, ast.Name) and node.target.id == "MACHINE_CATALOG":
                catalog = ast.literal_eval(node.value)
                return sum(len(machines) for machines in catalog.values())
    raise RuntimeError("Could not read MACHINE_CATALOG from main.py")


def check_distribution(distribution: Path) -> None:
    executable = distribution / "Omega_CNC_Processor.exe"
    smoke_executable = distribution / "Omega_CNC_Processor_Smoke.exe"
    if not executable.is_file():
        raise AssertionError(f"Packaged executable not found: {executable}")
    if not smoke_executable.is_file():
        raise AssertionError(
            f"Packaged diagnostic executable not found: {smoke_executable}"
        )
    if (distribution / "profiles.json").exists():
        raise AssertionError("Shared profiles.json must not be distributed")
    if (distribution / "machine_libraries").exists():
        raise AssertionError("Build output must start without machine libraries")

    with tempfile.TemporaryDirectory(prefix="omega-package-smoke-") as folder:
        clean_release = Path(folder) / distribution.name
        shutil.copytree(distribution, clean_release)
        smoke_log = clean_release / "packaging-smoke.log"
        environment = os.environ.copy()
        environment["OMEGA_PACKAGING_SMOKE_LOG"] = str(smoke_log)
        result = subprocess.run(
            [clean_release / smoke_executable.name, "--packaging-smoke-check"],
            cwd=clean_release,
            env=environment,
            timeout=60,
            check=False,
            capture_output=True,
            text=True,
        )
        if result.returncode:
            file_details = (
                smoke_log.read_text(encoding="utf-8", errors="replace")
                if smoke_log.is_file()
                else ""
            )
            details = "\n".join(
                part.strip()
                for part in (result.stdout, result.stderr, file_details)
                if part and part.strip()
            )
            if not details:
                details = (
                    "The packaged executable exited before it could write "
                    "diagnostic output."
                )
            raise AssertionError(
                f"Packaged app smoke check exited with {result.returncode}:\n"
                f"{details}"
            )

        if (clean_release / "profiles.json").exists():
            raise AssertionError("Packaged app created a shared profiles.json")
        profile_files = sorted(
            (clean_release / "machine_libraries").glob("*/profiles.json")
        )
        expected_count = _catalog_machine_count()
        if len(profile_files) != expected_count:
            raise AssertionError(
                f"Expected {expected_count} machine profiles.json files, "
                f"found {len(profile_files)}"
            )
        for profile_file in profile_files:
            data = json.loads(profile_file.read_text(encoding="utf-8"))
            if data.get("profiles") != {}:
                raise AssertionError(f"Non-empty machine library: {profile_file}")

    # The console-mode executable exists only to expose frozen-import failures
    # during this check; successful release folders contain the GUI app alone.
    smoke_executable.unlink()


if __name__ == "__main__":
    target = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else DEFAULT_DIST
    check_distribution(target)
    print(f"Windows package smoke check passed: {target}")