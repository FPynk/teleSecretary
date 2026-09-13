"""Tests for packaged LLM prompt files."""

from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

import _path  # noqa: F401
from tele_secretary.llm.prompts import load_system_prompt


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class SystemPromptTests(unittest.TestCase):
    """Verify the LLM system prompt can be read from its package folder."""

    def test_load_system_prompt_returns_the_system_prompt_markdown(self) -> None:
        """The loader reads the real prompt file instead of an empty or wrong file."""
        prompt_text = load_system_prompt()

        self.assertIn("# TeleSecretary Assistant", prompt_text)

    def test_installed_package_can_load_system_prompt(self) -> None:
        """Build and install a wheel, then load its bundled system prompt."""
        with tempfile.TemporaryDirectory() as temp_dir:
            temporary_root = Path(temp_dir)
            package_source = temporary_root / "package-source"
            wheel_directory = temporary_root / "wheels"
            installed_packages = temporary_root / "site-packages"
            package_source.mkdir()
            shutil.copy2(PROJECT_ROOT / "pyproject.toml", package_source)
            shutil.copy2(PROJECT_ROOT / "README.md", package_source)
            shutil.copytree(
                PROJECT_ROOT / "src",
                package_source / "src",
                ignore=shutil.ignore_patterns("__pycache__"),
            )

            build_result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "pip",
                    "wheel",
                    "--no-cache-dir",
                    "--no-build-isolation",
                    "--no-deps",
                    "--ignore-requires-python",
                    "--wheel-dir",
                    str(wheel_directory),
                    ".",
                ],
                capture_output=True,
                cwd=package_source,
                text=True,
            )
            self.assertEqual(
                build_result.returncode,
                0,
                f"Wheel build failed:\n{build_result.stdout}\n{build_result.stderr}",
            )

            wheel_files = list(wheel_directory.glob("tele_secretary-*.whl"))
            self.assertEqual(len(wheel_files), 1)

            install_result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "pip",
                    "install",
                    "--no-cache-dir",
                    "--no-deps",
                    "--ignore-requires-python",
                    "--target",
                    str(installed_packages),
                    str(wheel_files[0]),
                ],
                capture_output=True,
                text=True,
            )
            self.assertEqual(
                install_result.returncode,
                0,
                f"Wheel installation failed:\n{install_result.stdout}\n{install_result.stderr}",
            )

            environment = os.environ.copy()
            environment["PYTHONPATH"] = str(installed_packages)
            installed_prompt_check = (
                "from pathlib import Path\n"
                "import tele_secretary.llm.prompts as prompts\n"
                "from tele_secretary.llm.prompts import load_system_prompt\n"
                f"installed_packages = Path({str(installed_packages)!r}).resolve()\n"
                "assert Path(prompts.__file__).resolve().is_relative_to(installed_packages)\n"
                "assert '# TeleSecretary Assistant' in load_system_prompt()\n"
            )
            prompt_result = subprocess.run(
                [sys.executable, "-c", installed_prompt_check],
                capture_output=True,
                cwd=temporary_root,
                env=environment,
                text=True,
            )
            self.assertEqual(
                prompt_result.returncode,
                0,
                f"Installed prompt load failed:\n{prompt_result.stdout}\n{prompt_result.stderr}",
            )


if __name__ == "__main__":
    unittest.main()
