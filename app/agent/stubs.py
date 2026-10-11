"""Offline model scripts can inspect observations and decide their next action."""

from collections.abc import Callable, Sequence

from app.agent.protocol import ModelFailure, ModelRequest, ModelResponse

ModelStep = ModelResponse | Callable[[ModelRequest], ModelResponse]


class ScriptedModel:
    def __init__(self, steps: Sequence[ModelStep]) -> None:
        self.steps = tuple(steps)
        self.requests: list[ModelRequest] = []

    async def complete(self, request: ModelRequest) -> ModelResponse:
        index = len(self.requests)
        self.requests.append(ModelRequest.model_validate_json(request.model_dump_json()))
        if index >= len(self.steps):
            raise ModelFailure("FIXTURE_EXHAUSTED")
        step = self.steps[index]
        response = step(request) if callable(step) else step
        return ModelResponse.model_validate_json(response.model_dump_json())
