"""Message content: typed abstractions over the spec's content blocks.

https://modelcontextprotocol.io/specification/2026-07-28/server/prompts#data-types

Handlers build these, which are later converted to the protocol dicts.
"""

import base64
import mimetypes
from abc import ABC, abstractmethod
from collections.abc import Sequence
from dataclasses import dataclass, field
from os import PathLike
from pathlib import Path
from typing import TYPE_CHECKING, Literal, Self, overload

from . import _protocol


@dataclass(frozen=True, slots=True)
class Icon:
    """A visual identifier for display in user interfaces."""

    #: URL of the icon resource, or a base64 ``data:`` URI.
    src: str
    #: MIME type, when not implied by ``src``.
    mime_type: str | None = None
    #: Available sizes, e.g. ``("48x48",)``, or ``("any",)`` for scalable formats.
    sizes: Sequence[str] | None = None
    #: Intended background theme, when the icon needs a specific one.
    theme: Literal["light", "dark"] | None = None

    def _to_wire(self) -> _protocol.Icon:
        wire: _protocol.Icon = {"src": self.src}
        if self.mime_type is not None:
            wire["mimeType"] = self.mime_type
        if self.sizes is not None:
            wire["sizes"] = list(self.sizes)
        if self.theme is not None:
            wire["theme"] = self.theme
        return wire


@dataclass(frozen=True, slots=True)
class Content(ABC):
    """Base class of all message content blocks.

    Every subclass accepts these keyword arguments as hints for a client,
    e.g. :code:`TextContent("hi", audience=("user",), priority=0.5)`.
    """

    #: The intended audience:
    #:
    #: - ``"user"`` - Hints that the message should be shown to the user.
    #: - ``"assistant"`` - Hints that the message should be included in the
    #:   context for the AI assistant.
    audience: Sequence[_protocol.Role] | None = field(default=None, kw_only=True)
    #: How important this content is, from 0 (optional) to 1 (required).
    priority: float | None = field(default=None, kw_only=True)
    #: ISO 8601 timestamp of when the content was last modified.
    last_modified: str | None = field(default=None, kw_only=True)

    @abstractmethod
    def _to_wire(self) -> _protocol.ContentBlock:
        """Serialise to the specification's wire format."""

    def _annotations(self) -> _protocol.Annotations:
        annotations: _protocol.Annotations = {}
        if self.audience is not None:
            annotations["audience"] = list(self.audience)
        if self.priority is not None:
            annotations["priority"] = self.priority
        if self.last_modified is not None:
            annotations["lastModified"] = self.last_modified
        return annotations


@dataclass(frozen=True, slots=True)
class TextContent(Content):
    """Plain text content."""

    text: str

    def _to_wire(self) -> _protocol.TextContent:
        wire: _protocol.TextContent = {"type": "text", "text": self.text}
        if annotations := self._annotations():
            wire["annotations"] = annotations
        return wire


def _file_content(path: Path, prefix: str) -> tuple[bytes, str]:
    mime_type, _ = mimetypes.guess_type(path.name)
    if mime_type is None or not mime_type.startswith(prefix):
        raise ValueError(
            f"cannot determine a {prefix}* MIME type for {path.name!r};"
            " pass data and mime_type explicitly"
        )
    return path.read_bytes(), mime_type


@dataclass(frozen=True, slots=True)
class ImageContent(Content):
    """Image content from raw bytes."""

    data: bytes
    mime_type: str

    @classmethod
    def from_file(cls, path: str | PathLike[str]) -> Self:
        """Read an image file, guessing the MIME type from its extension.

        Reads synchronously: for large files outside startup code, prefer
        loading the bytes without blocking the event loop (e.g.
        :func:`asyncio.to_thread`) and constructing directly.
        """
        return cls(*_file_content(Path(path), "image/"))

    def _to_wire(self) -> _protocol.ImageContent:
        wire: _protocol.ImageContent = {
            "type": "image",
            "data": base64.b64encode(self.data).decode(),
            "mimeType": self.mime_type,
        }
        if annotations := self._annotations():
            wire["annotations"] = annotations
        return wire


@dataclass(frozen=True, slots=True)
class AudioContent(Content):
    """Audio content from raw bytes."""

    data: bytes
    mime_type: str

    @classmethod
    def from_file(cls, path: str | PathLike[str]) -> Self:
        """Read an audio file, guessing the MIME type from its extension.

        Reads synchronously; see :meth:`ImageContent.from_file`.
        """
        return cls(*_file_content(Path(path), "audio/"))

    def _to_wire(self) -> _protocol.AudioContent:
        wire: _protocol.AudioContent = {
            "type": "audio",
            "data": base64.b64encode(self.data).decode(),
            "mimeType": self.mime_type,
        }
        if annotations := self._annotations():
            wire["annotations"] = annotations
        return wire


@dataclass(frozen=True, slots=True)
class ResourceLink(Content):
    """A link to a server resource, without embedding its contents."""

    uri: str
    name: str
    title: str | None = None
    description: str | None = None
    mime_type: str | None = None
    size: int | None = None
    icons: Sequence[Icon] | None = None

    def _to_wire(self) -> _protocol.ResourceLink:
        wire: _protocol.ResourceLink = {
            "type": "resource_link",
            "uri": self.uri,
            "name": self.name,
        }
        if self.title is not None:
            wire["title"] = self.title
        if self.description is not None:
            wire["description"] = self.description
        if self.mime_type is not None:
            wire["mimeType"] = self.mime_type
        if self.size is not None:
            wire["size"] = self.size
        if self.icons is not None:
            wire["icons"] = [icon._to_wire() for icon in self.icons]
        if annotations := self._annotations():
            wire["annotations"] = annotations
        return wire


@dataclass(frozen=True, slots=True)
class EmbeddedResource(Content):
    """A server resource embedded directly into a message.

    Do not create directly. Use :meth:`.MCPRequest.embed_resource`.
    """

    uri: str
    text: str | None = field(default=None, kw_only=True)
    blob: bytes | None = field(default=None, kw_only=True)
    mime_type: str | None = field(default=None, kw_only=True)

    if TYPE_CHECKING:
        # Static form of the __post_init__ check: exactly one of text/blob.
        @overload
        def __init__(
            self,
            uri: str,
            *,
            text: str,
            mime_type: str | None = None,
            audience: Sequence[_protocol.Role] | None = None,
            priority: float | None = None,
            last_modified: str | None = None,
        ) -> None: ...
        @overload
        def __init__(
            self,
            uri: str,
            *,
            blob: bytes,
            mime_type: str | None = None,
            audience: Sequence[_protocol.Role] | None = None,
            priority: float | None = None,
            last_modified: str | None = None,
        ) -> None: ...
        def __init__(
            self,
            uri: str,
            *,
            text: str | None = None,
            blob: bytes | None = None,
            mime_type: str | None = None,
            audience: Sequence[_protocol.Role] | None = None,
            priority: float | None = None,
            last_modified: str | None = None,
        ) -> None: ...

    def __post_init__(self) -> None:
        if (self.text is None) == (self.blob is None):
            raise ValueError("exactly one of text or blob is required")

    def _to_wire(self) -> _protocol.EmbeddedResource:
        resource: _protocol.TextResourceContents | _protocol.BlobResourceContents
        if self.text is not None:
            resource = {"uri": self.uri, "text": self.text}
        else:
            assert self.blob is not None
            resource = {"uri": self.uri, "blob": base64.b64encode(self.blob).decode()}
        if self.mime_type is not None:
            resource["mimeType"] = self.mime_type
        wire: _protocol.EmbeddedResource = {"type": "resource", "resource": resource}
        if annotations := self._annotations():
            wire["annotations"] = annotations
        return wire
