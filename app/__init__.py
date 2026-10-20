"""
E-commerce Backend API
---------------------
FastAPI-based e-commerce backend implementing the technical specification
for a comprehensive online store system.
"""

import tomllib
from pathlib import Path

# The version lives in one place: `version` in pyproject.toml. Change it with
# `uv version --bump patch|minor|major` (or `uv version X.Y.Z`), which also
# updates uv.lock; editing it by hand works too, followed by `uv lock` (CI
# fails `uv lock --check` otherwise). It is read from the file rather than
# from installed package metadata because the Docker image never installs the
# project itself (`uv sync --no-install-project`); pyproject.toml is copied
# next to the code.
__version__: str = tomllib.loads(
    (Path(__file__).resolve().parent.parent / "pyproject.toml").read_text(encoding="utf-8")
)["project"]["version"]
__author__ = "E-commerce Team"
