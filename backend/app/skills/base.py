from abc import ABC, abstractmethod
from typing import Any, AsyncGenerator, Dict, List, Optional

from pydantic import BaseModel, Field


class SkillContext(BaseModel):
    session_id: str
    user_id: str
    message: str
    history: List[Dict[str, str]] = Field(default_factory=list)
    metadata: Dict[str, Any] = Field(default_factory=dict)


class SkillResult(BaseModel):
    success: bool
    message: str
    data: Optional[Dict[str, Any]] = None
    need_continue: bool = False


class BaseSkill(ABC):
    @property
    @abstractmethod
    def name(self) -> str:
        raise NotImplementedError

    @property
    @abstractmethod
    def description(self) -> str:
        raise NotImplementedError

    @property
    def parameters(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {"query": {"type": "string", "description": "User input"}},
            "required": ["query"],
        }

    async def execute(self, context: SkillContext, **kwargs: Any) -> SkillResult:
        raise NotImplementedError(f"Skill {self.name} does not implement execute()")

    async def execute_stream(
        self,
        context: SkillContext,
        **kwargs: Any,
    ) -> AsyncGenerator[Dict[str, Any], None]:
        raise NotImplementedError(f"Skill {self.name} does not implement execute_stream()")
        yield {}

    async def validate(self, **kwargs: Any) -> bool:
        return True

    def to_tool_definition(self) -> Dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }

