"""
Minimal API-key test.

It reads your key from the .env file in this folder, sends ONE short
message to the model, and prints the reply. If you see a reply, your key works.

Run it with:   python test_key.py
"""

import os
import sys
from pathlib import Path


def load_env():
    """Tiny .env reader so we don't need extra packages for this step."""
    env_path = Path(__file__).parent / ".env"
    if not env_path.exists():
        sys.exit(
            "\n[!] No .env file found.\n"
            "    Copy .env.example to .env and paste your key into it.\n"
        )
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ[key.strip()] = value.strip()


def looks_real(value: str) -> bool:
    return bool(value) and "paste-your-key" not in value


def main():
    load_env()

    anthropic_key = os.environ.get("ANTHROPIC_API_KEY", "")
    openai_key = os.environ.get("OPENAI_API_KEY", "")

    if looks_real(anthropic_key):
        print("Using Anthropic (Claude)...\n")
        try:
            from anthropic import Anthropic
        except ImportError:
            sys.exit("[!] Run:  pip install anthropic")
        client = Anthropic(api_key=anthropic_key)
        msg = client.messages.create(
            model="claude-opus-4-8",
            max_tokens=100,
            messages=[{"role": "user", "content": "Say hello in one short sentence."}],
        )
        print("Model replied:", msg.content[0].text)

    elif looks_real(openai_key):
        print("Using OpenAI (GPT)...\n")
        try:
            from openai import OpenAI
        except ImportError:
            sys.exit("[!] Run:  pip install openai")
        client = OpenAI(api_key=openai_key)
        resp = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": "Say hello in one short sentence."}],
        )
        print("Model replied:", resp.choices[0].message.content)

    else:
        sys.exit(
            "[!] No real key found in .env.\n"
            "    Open .env and paste your key after ANTHROPIC_API_KEY= (or OPENAI_API_KEY=)."
        )

    print("\n✅ Success — your key works!")


if __name__ == "__main__":
    main()
