import base64
from pathlib import Path

import pytest

from aiohttp_mcp import (
    AudioContent,
    EmbeddedResource,
    Icon,
    ImageContent,
    ResourceLink,
    TextContent,
)


def test_text() -> None:
    assert TextContent("hi")._to_wire() == {"type": "text", "text": "hi"}


def test_annotation_kwargs() -> None:
    content = TextContent(
        "hi", audience=("user",), priority=0.5, last_modified="2026-10-17T00:00:00Z"
    )
    assert content._to_wire() == {
        "type": "text",
        "text": "hi",
        "annotations": {
            "audience": ["user"],
            "priority": 0.5,
            "lastModified": "2026-10-17T00:00:00Z",
        },
    }


def test_image() -> None:
    wire = ImageContent(b"\x00\x01", "image/png")._to_wire()
    assert wire == {"type": "image", "data": "AAE=", "mimeType": "image/png"}
    assert base64.b64decode(wire["data"]) == b"\x00\x01"


def test_audio() -> None:
    assert AudioContent(b"\x00", "audio/wav")._to_wire() == {
        "type": "audio",
        "data": "AA==",
        "mimeType": "audio/wav",
    }


@pytest.mark.parametrize(
    ("cls", "suffix", "mime_types"),
    (
        (ImageContent, "jpeg", {"image/jpeg"}),
        (ImageContent, "png", {"image/png"}),
        # Windows: wav, Other OSes: x-wav
        (AudioContent, "wav", {"audio/x-wav", "audio/wav"}),
    ),
)
def test_from_file(
    tmp_path: Path,
    cls: type[ImageContent | AudioContent],
    suffix: str,
    mime_types: set[str],
) -> None:
    file = tmp_path / f"foo.{suffix}"
    file.write_bytes(b"\xffdata")
    content = cls.from_file(file)
    assert content.data == b"\xffdata"
    assert content.mime_type in mime_types


def test_from_file_accepts_str_path(tmp_path: Path) -> None:
    file = tmp_path / "bar.png"
    file.write_bytes(b"x")
    assert ImageContent.from_file(str(file)).mime_type == "image/png"


@pytest.mark.parametrize("name", ("foo.txt", "foo.wav", "foo"))
def test_image_from_file_wrong_type(tmp_path: Path, name: str) -> None:
    file = tmp_path / name
    file.write_bytes(b"x")
    with pytest.raises(ValueError, match="MIME type"):
        ImageContent.from_file(file)


def test_resource_link() -> None:
    assert ResourceLink("file:///x.rs", "x.rs")._to_wire() == {
        "type": "resource_link",
        "uri": "file:///x.rs",
        "name": "x.rs",
    }
    full = ResourceLink(
        "file:///x.rs",
        "x.rs",
        title="Main",
        description="Entry point",
        mime_type="text/x-rust",
        size=120,
    )._to_wire()
    assert full["title"] == "Main"
    assert full["mimeType"] == "text/x-rust"
    assert full["size"] == 120


def test_embedded_resource_text() -> None:
    wire = EmbeddedResource(
        "resource://example", text="content", mime_type="text/plain"
    )._to_wire()
    assert wire == {
        "type": "resource",
        "resource": {
            "uri": "resource://example",
            "text": "content",
            "mimeType": "text/plain",
        },
    }


def test_embedded_resource_blob() -> None:
    wire = EmbeddedResource("resource://example", blob=b"\x00")._to_wire()
    assert wire["resource"] == {"uri": "resource://example", "blob": "AA=="}


@pytest.mark.parametrize("kwargs", ({}, {"text": "x", "blob": b"y"}))
def test_embedded_resource_text_xor_blob(kwargs: dict[str, object]) -> None:
    with pytest.raises(ValueError, match="exactly one"):
        EmbeddedResource("resource://example", **kwargs)  # type: ignore[call-overload]

def test_content_immutable() -> None:
    content = TextContent("hi")
    with pytest.raises(AttributeError):
        content.text = "changed"  # type: ignore[misc]


def test_icon() -> None:
    assert Icon("https://example.com/i.png")._to_wire() == {
        "src": "https://example.com/i.png"
    }
    assert Icon(
        "https://example.com/i.svg",
        mime_type="image/svg+xml",
        sizes=("any",),
        theme="dark",
    )._to_wire() == {
        "src": "https://example.com/i.svg",
        "mimeType": "image/svg+xml",
        "sizes": ["any"],
        "theme": "dark",
    }
