"""Model boundary. Only the existing OpenAI adapter is supported in this release."""
from dataclasses import dataclass
from typing import Protocol, Callable

@dataclass(frozen=True)
class ModelCapabilities:
    tools: bool = True
    structured_output: bool = True
    reasoning_continuation: bool = True

@dataclass(frozen=True)
class ModelDecision:
    checkpoint: dict
    tool_calls: list[dict]

class ModelProvider(Protocol):
    capabilities: ModelCapabilities
    def decide(self, *, model: str, effort: str, tools: list, prepare_input: Callable) -> ModelDecision: ...
    def structured(self, prompt, schema, **options): ...

def current_provider() -> ModelProvider:
    import os
    if os.getenv('MODEL_PROVIDER', 'openai') != 'openai':
        raise ValueError('This release supports MODEL_PROVIDER=openai; additional adapters are deferred.')
    from .openai import OpenAIProvider
    return OpenAIProvider()
