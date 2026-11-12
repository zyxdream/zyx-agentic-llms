"""Provider-specific wire format and reasoning checkpoints stay inside this adapter."""
from . import ModelCapabilities, ModelDecision

class OpenAIProvider:
    capabilities = ModelCapabilities()

    def structured(self, prompt, schema, **options):
        from backend.ai.base import chat_completion_parse
        return chat_completion_parse(prompt, schema, **options)

    def decide(self, *, model, effort, tools, prepare_input):
        from backend.ai.base import responses_completion, _effective_reasoning_effort
        request = dict(model=model, tools=tools, tool_choice='required', parallel_tool_calls=False,
                       store=False, include=['reasoning.encrypted_content'])
        effort = _effective_reasoning_effort(model, effort)
        if effort: request['reasoning'] = {'effort': effort}
        request['input'] = prepare_input(request)
        response = responses_completion(request)
        if response is None: raise ValueError('The controller provider returned no response.')
        raw = response.model_dump(mode='json', exclude_none=True) if hasattr(response, 'model_dump') else response
        output = raw.get('output', [])
        calls = [item for item in output if item.get('type') == 'function_call']
        if not calls: raise ValueError('The controller returned no application action.')
        return ModelDecision(checkpoint={'output': output}, tool_calls=calls)
