"""Token budgeting for the replayed conversation.

The browser holds the transcript and sends it back with every question, so this
module answers one question for each request: does the system prompt, the
retrieved schema, the replayed history and the new question still fit in the
context the model is loaded with?

When it does not, the run is refused and the user starts a new chat. That is a
deliberate choice over silently dropping the oldest turns: an assistant that
quietly forgets which case you were discussing gives confidently wrong answers,
while one that stops is merely inconvenient.

Token counts are ESTIMATES. There is no tokenizer here — adding one means
shipping the model's vocabulary to the backend — so text is measured by script.
The constants below deliberately over-count: being wrong high wastes a little
window, while being wrong low hands the overflow to LM Studio, which truncates
from the START of the prompt. That drops the schema, not the small talk, and
the model starts inventing column names instead of admitting it lost context.
"""

import unicodedata
from dataclasses import dataclass

from app.config import (
    CONTEXT_MAX_TURNS,
    CONTEXT_OUTPUT_RESERVE_TOKENS,
    CONTEXT_WINDOW_TOKENS,
)

# Characters per token, by script. Arabic sits at roughly 2 in the tokenizers
# these models use; Latin text and the SQL/identifier soup around it closer to
# 4. Both are rounded down (i.e. more tokens) on purpose — see module docstring.
_ARABIC_CHARS_PER_TOKEN = 1.8
_LATIN_CHARS_PER_TOKEN = 3.2

# Per-message framing the chat template adds around every turn (role markers,
# separators). Small, but it is charged once per message and history is many
# messages.
_TOKENS_PER_MESSAGE = 4


@dataclass
class Budget:
    """What the next request would cost, and what there is to spend."""

    used_tokens: int
    limit_tokens: int
    history_turns: int

    @property
    def exhausted(self) -> bool:
        return self.used_tokens >= self.limit_tokens

    @property
    def fraction(self) -> float:
        return 0.0 if self.limit_tokens <= 0 else self.used_tokens / self.limit_tokens


def _is_arabic(char: str) -> bool:
    return "؀" <= char <= "ۿ" or "ﭐ" <= char <= "﻿"


def estimate_tokens(text: str) -> int:
    """Approximate the token cost of a string, rounded up and script-aware."""
    if not text:
        return 0

    normalized = unicodedata.normalize("NFC", text)
    arabic = sum(1 for char in normalized if _is_arabic(char))
    other = len(normalized) - arabic

    estimate = arabic / _ARABIC_CHARS_PER_TOKEN + other / _LATIN_CHARS_PER_TOKEN
    return int(estimate) + 1  # round up; never report a non-empty string as free


def estimate_messages(messages: list[dict]) -> int:
    """Token cost of a chat-completions message array, framing included."""
    return sum(
        estimate_tokens(message.get("content", "")) + _TOKENS_PER_MESSAGE
        for message in messages
    )


def limit_tokens() -> int:
    """Tokens available for prompt content, once the reply's head-room is set aside."""
    return max(0, CONTEXT_WINDOW_TOKENS - CONTEXT_OUTPUT_RESERVE_TOKENS)


def trim_history(history: list) -> list:
    """Keep the most recent turns only.

    A hard turn cap sits in front of the token budget so that a client replaying
    an unbounded transcript is cheap to reject — the token estimate never runs
    over a hundred turns of text.
    """
    return history[-CONTEXT_MAX_TURNS:] if CONTEXT_MAX_TURNS > 0 else []


def measure(messages: list[dict], history_turns: int) -> Budget:
    """Cost the fully-built prompt against the window."""
    return Budget(
        used_tokens=estimate_messages(messages),
        limit_tokens=limit_tokens(),
        history_turns=history_turns,
    )
