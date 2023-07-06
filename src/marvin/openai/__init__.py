from typing import Callable, Dict, List, Union, TypeVar, Type, Any, Optional, Set
import functools
from pydantic import BaseModel, validate_arguments
from marvin.models.messages import Message
from marvin.prompts import render_prompts
from marvin.utilities.types import function_to_schema
from fastapi import APIRouter
import openai
import json

T = TypeVar("T")
A = TypeVar("A")


def write_code(
    language: str,
    filename: str,
    name: str,
    docstring: str,
    code: str,
) -> str:
    """Accepts and checks staff engineer quality written `code` in `language`"""
    return (language, filename, name, docstring, code)


class OpenAIFunction:
    """
    Represents an OpenAI function with metadata and
    functionality for rendering prompts and queries.

    Attributes:
        fn (Callable): The underlying function to be encapsulated.
        name (str): The name of the OpenAI function.
        description (str): A description of the OpenAI function.
        parameters (List[Dict[str, str]]): The parameters of the OpenAI function.

    Methods:
        render: Renders prompts and returns a formatted message.
        prompt: Alias for the render method.
        query: Renders a query and returns a formatted message.
    """

    def __init__(
        self, *, fn: Callable = None, name: str = None, description: str = None
    ) -> None:
        """
        Initializes an OpenAIFunction instance.

        Args:
            fn (Callable, optional): The underlying function to be encapsulated.
            name (str, optional): The name of the OpenAI function.
            description (str, optional): A description of the OpenAI function.
        """
        self.fn = fn
        schema = function_to_schema(self.fn)
        __name__, parameters = schema.pop("title"), schema

        self.parameters = parameters
        self.description = description or fn.__doc__
        self.name = name or __name__

        super().__init__()

    @property
    def schema(self) -> Dict[str, Union[str, List[Dict[str, str]]]]:
        """
        Returns the schema of the OpenAI function.

        Returns:
            Dict[str, Union[str, List[Dict[str, str]]]]: Schema of the OpenAI function.
        """
        return {
            "name": self.name,
            "description": self.description,
            "parameters": self.parameters,
        }

    @validate_arguments
    def render(
        self,
        messages: List[Message] = [],
        *args,
        function_call: Union[str, Dict[str, str]] = None,
        **kwargs,
    ) -> Message:
        """
        Renders prompts using the OpenAI function and returns a formatted message.

        Args:
            messages (List[Message], optional): List of messages in the conversation.
            function_call (Union[str, Dict[str, str]], optional): Function call details.

        Returns:
            Message: The formatted message containing prompts and function details.
        """
        return {
            "messages": [
                message.as_chat_message() for message in render_prompts(messages)
            ],
            "functions": [self.schema],
            "function_call": function_call or {"name": self.name},
        }

    def handle_response(self, response) -> Dict:
        return json.loads(response.choices[0].message.function_call.get("arguments"))

    @validate_arguments
    def prompt(
        self,
        messages: List[Message] = [],
        *args,
        function_call: Union[str, Dict[str, str]] = None,
        **kwargs,
    ) -> Message:
        """
        Alias for the render method.

        Args:
            messages (List[Message], optional): List of messages in the conversation.
            function_call (Union[str, Dict[str, str]], optional): Function call details.

        Returns:
            Message: The formatted message containing prompts and function details.
        """
        return self.render(messages, *args, function_call=function_call, **kwargs)

    @validate_arguments
    def query(
        self,
        query: str,
        *args,
        engine=functools.partial(openai.ChatCompletion.create, model="gpt-3.5-turbo"),
        function_call: Union[str, Dict[str, str]] = None,
        **kwargs,
    ) -> Message:
        return self.fn(
            **self.handle_response(
                engine(
                    **self.render(
                        messages=[{"role": "user", "content": query}],
                        *args,
                        function_call=function_call,
                        **kwargs,
                    )
                )
            )
        )

    def code(
        self,
        language: str = "python",
        save=False,
    ) -> str:
        return self.__class__(fn=write_code).prompt(
            [
                {
                    "role": "user",
                    "content": (
                        f"A function in {language} described as the following:"
                        f" {self.schema}"
                    ),
                }
            ]
        )


def openai_fn(fn: Callable[[A], T] = None) -> Callable[[A], T]:
    if fn is None:
        functools.partial(openai_fn)
    else:
        fn.__openai__ = OpenAIFunction(fn=fn)
        for method in dir(fn.__openai__):
            is_method_private = method.startswith("__")
            if not is_method_private:
                setattr(fn, method, getattr(fn.__openai__, method))
    return fn


class FunctionList(list):
    @property
    def schema(self):
        return [fn.schema for fn in self]


class OpenAIFunctionRegistry(APIRouter):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.registered_routers: Set[Type[APIRouter]] = set()

    @property
    def functions(self):
        return FunctionList([route.endpoint for route in self.routes])

    @property
    def schema(self):
        return {"functions": self.functions.schema, "function_call": "auto"}

    def include(self, registry: "OpenAIFunctionRegistry", *args, **kwargs):
        # Adds some 100-IQ idempotency.
        if type(registry) not in self.registered_routers:
            super().include_router(registry, *args, **kwargs)
            self.registered_routers.add(type(registry))

    @validate_arguments
    def prompt(
        self,
        messages: List[Message] = [],
        *args,
        function_call: Union[str, Dict[str, str]] = None,
        **kwargs,
    ) -> Message:
        return {
            **self.functions[0].prompt(messages, *args, **kwargs),
            **self.schema,
            **({"function_call": function_call} if function_call else {}),
        }

    def register(self, fn: Optional[Callable] = None, **kwargs: Any) -> Callable:
        def decorator(fn: Callable, *args) -> Callable:
            fn = openai_fn(fn)
            self.add_api_route(
                **{
                    **{
                        "name": fn.name,
                        "path": f"/{fn.name}",
                        "endpoint": fn,
                        "description": fn.description,
                        "methods": ["POST"],
                    },
                    **kwargs,
                }
            )
            return fn

        if fn:
            # if the decorator was called with parentheses
            return decorator(fn)
        else:
            # else, return the decorator to be called later
            return decorator
