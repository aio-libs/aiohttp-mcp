from aiohttp_mcp import (
    Content,
    EmbeddedResource,
    Message,
    Prompt,
    PromptRegistry,
    TextContent,
)


def prompts_setup() -> None:
    from typing import Annotated

    from aiohttp import web
    from aiohttp_mcp import Doc, PromptRegistry, setup

    async def code_review(
        code: Annotated[str, Doc("The code to review")],
        focus: Annotated[str, Doc("What to focus the review on")] = "general quality",
    ) -> str:
        """Request Code Review.

        Asks the LLM to analyze code quality and suggest improvements.
        """
        return f"Please review this code, focusing on {focus}:\n\n{code}"

    app = web.Application()
    setup(app, prompts=PromptRegistry(code_review))
    web.run_app(app, handler_cancellation=True)


def multi_modal() -> None:
    from pathlib import Path

    from aiohttp_mcp import Content, ImageContent, TextContent

    async def describe_logo() -> tuple[Content, ...]:
        """Ask for a description of the project logo."""
        return (
            ImageContent.from_file(Path("assets/logo.jpeg")),
            TextContent("Describe this logo for the website's alt text."),
        )


async def style_guide() -> tuple[Content, ...]:
    """Rewrite following the house style guide."""
    return (
        TextContent("Rewrite the draft to follow the style guide."),
        EmbeddedResource(
            "resource://style-guide",
            text="Use sentence case. Avoid the passive voice.",
            mime_type="text/plain",
            # For the model, not shown to the user, and essential.
            audience=("assistant",),
            priority=1.0,
        ),
    )


async def plan_feature(feature: str) -> tuple[Message, ...]:
    """Plan a feature, with a worked example of the expected output."""
    return (
        Message(TextContent("Plan: dark mode")),
        Message(TextContent("1. ..."), role="assistant"),
        Message(TextContent(f"Plan: {feature}")),
    )


def scoped_prompt() -> None:
    async def rotate_keys() -> str:
        """Walk through rotating the deployment keys."""
        return "Rotate the deployment keys, one service at a time."

    prompt = Prompt(rotate_keys, scopes=("mcp:admin",))
    registry = PromptRegistry(prompt)
