from pathlib import Path

def load_system_prompt() -> str:
    """Return the packaged system prompt for the LLM agent."""
    prompt_text = Path(__file__).with_name("system.md").read_text(encoding="utf-8")
    return prompt_text