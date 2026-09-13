"""Tests for packaged LLM prompt files."""

from __future__ import annotations

import unittest

import _path  # noqa: F401
from tele_secretary.llm.prompts import load_system_prompt


class SystemPromptTests(unittest.TestCase):
    """Verify the LLM system prompt can be read from its package folder."""

    def test_load_system_prompt_returns_the_system_prompt_markdown(self) -> None:
        """The loader reads the real prompt file instead of an empty or wrong file."""
        prompt_text = load_system_prompt()

        self.assertIn("# TeleSecretary Assistant", prompt_text)


if __name__ == "__main__":
    unittest.main()
