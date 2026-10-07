"""Install each built artifact outside the checkout and exercise public imports."""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
import tomllib
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("artifacts", nargs="*", type=Path)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    version = tomllib.loads((root / "pyproject.toml").read_text())["project"]["version"]
    artifacts = args.artifacts or [
        root / "dist" / f"coloph_agent_runtime-{version}-py3-none-any.whl",
        root / "dist" / f"coloph_agent_runtime-{version}.tar.gz",
    ]
    if len(artifacts) != 2:
        raise RuntimeError("Supply exactly one wheel and one source distribution")
    for artifact in artifacts:
        with tempfile.TemporaryDirectory() as directory:
            temporary = Path(directory)
            venv = temporary / "venv"
            subprocess.run(["uv", "venv", "--python", sys.executable, str(venv)], check=True, timeout=60)
            python = venv / "bin/python"
            subprocess.run(
                ["uv", "pip", "install", "--python", str(python), str(artifact.resolve())], check=True, timeout=180
            )
            example = temporary / "application.py"
            shutil.copyfile(root / "examples/application.py", example)
            env = {key: value for key, value in os.environ.items() if key not in {"PYTHONPATH", "VIRTUAL_ENV"}}
            subprocess.run([str(python), "-I", str(example)], cwd=temporary, env=env, check=True, timeout=60)
    print("Wheel and source artifact smoke passed")


if __name__ == "__main__":
    main()
