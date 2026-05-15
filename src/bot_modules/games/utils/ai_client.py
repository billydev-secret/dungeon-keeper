import logging
import os
from anthropic import AsyncAnthropic, APIError, APITimeoutError

log = logging.getLogger(__name__)

_client: AsyncAnthropic | None = None


def get_client() -> AsyncAnthropic:
    global _client
    if _client is None:
        _client = AsyncAnthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
    return _client


async def generate_text(
    system_prompt: str,
    user_prompt: str,
    model: str = "claude-haiku-4-5-20251001",
    max_tokens: int = 200,
    temperature: float = 0.9,
) -> str | None:
    """
    Call Anthropic messages API. Returns the response text or None on error.
    """
    try:
        client = get_client()
        response = await client.messages.create(
            model=model,
            system=system_prompt,
            messages=[{"role": "user", "content": user_prompt}],
            max_tokens=max_tokens,
            temperature=temperature,
        )
        parts = [b.text for b in response.content if getattr(b, "type", None) == "text"]
        return "".join(parts).strip() or None
    except (APIError, APITimeoutError) as e:
        log.error("Anthropic API error: %s", e)
        return None
    except Exception as e:
        log.error("Unexpected Anthropic error: %s", e)
        return None


OPENAI_ERROR_MSG = (
    "Couldn't generate a question right now — try again or write one manually!"
)
