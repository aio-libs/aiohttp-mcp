"""Elicitation: requesting user input from the client."""

import asyncio
import hashlib
import heapq
import json
import logging
import secrets
import time
from abc import ABC, abstractmethod
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from functools import lru_cache
from typing import TYPE_CHECKING, Annotated, ClassVar, Final, Literal, TypeVar

from pydantic import Field, TypeAdapter, ValidationError
from yarl import URL

from . import _protocol
from ._auth_server import _Signer
from ._protocol import (
    INTERNAL_ERROR,
    INVALID_PARAMS,
    INVALID_REQUEST,
    META_CLIENT_CAPABILITIES,
    MISSING_CLIENT_CAPABILITY,
    ElicitRequest,
    ElicitRequestFormParams,
    ElicitRequestURLParams,
    InputRequiredResult,
    MCPError,
)

if TYPE_CHECKING:
    from ._server import MCPServer
    from ._transport import MCPRequest

logger = logging.getLogger("aiohttp_mcp")

_STATE_TTL: Final = 600.0
_MAX_PENDING_FORMS: Final = 1024
_FORM_WAIT: Final = 10.0
_WireT = TypeVar("_WireT", bound=_protocol._SchemaCommon)
_T = TypeVar("_T")


# TODO(PY315): Replace type with TypeForm
@lru_cache(maxsize=1024)  # https://github.com/python/typeshed/issues/6347
def _adapter(hint: type[_T]) -> TypeAdapter[_T]:  # type: ignore[misc]
    return TypeAdapter(hint)


# TODO(PY315): Replace type with TypeForm
def _check(hint: type[_T], value: object) -> _T:
    """Validate a submitted JSON value; raises ValueError when it does not fit.

    pydantic's strict mode matches JSON Schema semantics for every type a
    form field can use (no ``"5"`` -> 5 or ``5.0`` -> 5 coercion; booleans
    are never numbers), so the parsed value is validated directly.
    """
    try:
        # https://github.com/python/mypy/issues/11470
        return _adapter(hint).validate_python(value, strict=True)  # type: ignore[arg-type,no-any-return]
    except ValidationError as exc:
        raise ValueError(exc.errors(include_url=False)[0]["msg"]) from None


@dataclass(frozen=True, slots=True, kw_only=True)
class FieldSchema(ABC):
    """Base class of the form field schemas.

    :param title: Optional display name of the field.
    :param description: Optional description of the field.
    :param required: Whether the user must provide a value.
    """

    title: str | None = None
    description: str | None = None
    required: bool = True

    def _common_wire(self, wire: _WireT) -> _WireT:
        if self.title is not None:
            wire["title"] = self.title
        if self.description is not None:
            wire["description"] = self.description
        return wire

    @abstractmethod
    def _to_wire(self) -> _protocol.PrimitiveSchemaDefinition:
        """The field as a JSON Schema fragment."""

    @abstractmethod
    def _validate(self, value: object) -> object:
        """Return the value if it satisfies this schema, else raise ValueError."""


@dataclass(frozen=True, slots=True, kw_only=True)
class StringSchema(FieldSchema):
    """A text field.

    :param min_length: Minimum accepted length.
    :param max_length: Maximum accepted length.
    :param format: A format hint for the client (not validated server-side).
    :param default: Default value for the field.
    """

    min_length: int | None = None
    max_length: int | None = None
    format: Literal["email", "uri", "date", "date-time"] | None = None
    default: str | None = None

    def _to_wire(self) -> _protocol.StringSchema:
        wire: _protocol.StringSchema = {"type": "string"}
        if self.min_length is not None:
            wire["minLength"] = self.min_length
        if self.max_length is not None:
            wire["maxLength"] = self.max_length
        if self.format is not None:
            wire["format"] = self.format
        if self.default is not None:
            wire["default"] = self.default
        return self._common_wire(wire)

    def _validate(self, value: object) -> str:
        hint = Annotated[str, Field(min_length=self.min_length, max_length=self.max_length)]
        return _check(hint, value)


@dataclass(frozen=True, slots=True, kw_only=True)
class IntegerSchema(FieldSchema):
    """A whole-number field.

    :param minimum: Minimum accepted value.
    :param maximum: Maximum accepted value.
    :param default: Default value for the field.
    """

    minimum: int | None = None
    maximum: int | None = None
    default: int | None = None

    def _to_wire(self) -> _protocol.NumberSchema:
        wire: _protocol.NumberSchema = {"type": "integer"}
        if self.minimum is not None:
            wire["minimum"] = self.minimum
        if self.maximum is not None:
            wire["maximum"] = self.maximum
        if self.default is not None:
            wire["default"] = self.default
        return self._common_wire(wire)

    def _validate(self, value: object) -> int:
        return _check(Annotated[int, Field(ge=self.minimum, le=self.maximum)], value)  # type: ignore[arg-type]


@dataclass(frozen=True, slots=True, kw_only=True)
class NumberSchema(FieldSchema):
    """A numeric field.

    :param minimum: Minimum accepted value.
    :param maximum: Maximum accepted value.
    :param default: Default value for the field.
    """

    minimum: float | None = None
    maximum: float | None = None
    default: float | None = None

    def _to_wire(self) -> _protocol.NumberSchema:
        wire: _protocol.NumberSchema = {"type": "number"}
        if self.minimum is not None:
            wire["minimum"] = self.minimum
        if self.maximum is not None:
            wire["maximum"] = self.maximum
        if self.default is not None:
            wire["default"] = self.default
        return self._common_wire(wire)

    def _validate(self, value: object) -> float:
        return _check(Annotated[float, Field(ge=self.minimum, le=self.maximum)], value)  # type: ignore[arg-type]


@dataclass(frozen=True, slots=True, kw_only=True)
class BooleanSchema(FieldSchema):
    """A yes/no field.

    :param default: Default value for the field.
    """

    default: bool | None = None

    def _to_wire(self) -> _protocol.BooleanSchema:
        wire: _protocol.BooleanSchema = {"type": "boolean"}
        if self.default is not None:
            wire["default"] = self.default
        return self._common_wire(wire)

    def _validate(self, value: object) -> bool:
        return _check(bool, value)


@dataclass(frozen=True, slots=True, kw_only=True)
class EnumSchema(FieldSchema):
    """A single selection from a fixed set of options.

    :param options: The values to choose from: a sequence, or a mapping of values
        to display titles.
    :param default: Default value for the field.
    """

    options: Sequence[str] | Mapping[str, str] = ()
    default: str | None = None

    def __post_init__(self) -> None:
        if not self.options:
            raise ValueError("options must not be empty")

    def _to_wire(self) -> _protocol.SingleSelectEnumSchema:
        wire: _protocol.SingleSelectEnumSchema
        if isinstance(self.options, Mapping):
            wire = {
                "type": "string",
                "oneOf": tuple({"const": k, "title": v} for k, v in self.options.items()),
            }
        else:
            wire = {"type": "string", "enum": tuple(self.options)}
        if self.default is not None:
            wire["default"] = self.default
        return self._common_wire(wire)

    def _validate(self, value: object) -> str:
        return _check(Literal[tuple(self.options)], value)  # type: ignore[arg-type]


@dataclass(frozen=True, slots=True, kw_only=True)
class MultiSelectEnumSchema(FieldSchema):
    """A selection of several options from a fixed set.

    The submitted value is a list of the selected options.

    :param options: The values to choose from: a sequence, or a mapping of values
        to display titles.
    :param min_items: Minimum number of selections.
    :param max_items: Maximum number of selections.
    :param default: Default values for the field.
    """

    options: Sequence[str] | Mapping[str, str] = ()
    min_items: int | None = None
    max_items: int | None = None
    default: Sequence[str] | None = None

    def __post_init__(self) -> None:
        if not self.options:
            raise ValueError("options must not be empty")

    def _to_wire(self) -> _protocol.MultiSelectEnumSchema:
        wire: _protocol.MultiSelectEnumSchema
        if isinstance(self.options, Mapping):
            wire = {
                "type": "array",
                "items": {
                    "anyOf": tuple(
                        {"const": k, "title": v} for k, v in self.options.items()
                    )
                },
            }
        else:
            wire = {
                "type": "array",
                "items": {"type": "string", "enum": tuple(self.options)},
            }
        if self.min_items is not None:
            wire["minItems"] = self.min_items
        if self.max_items is not None:
            wire["maxItems"] = self.max_items
        if self.default is not None:
            wire["default"] = tuple(self.default)
        return self._common_wire(wire)

    def _validate(self, value: object) -> tuple[str, ...]:
        # Static typing can't work here, this is for runtime typing only.
        hint = Annotated[  # type: ignore[valid-type]
            list[Literal[tuple(self.options)]],  # type: ignore[misc]
            Field(min_length=self.min_items, max_length=self.max_items),
        ]
        return tuple(_check(hint, value))  # type: ignore[arg-type]


@dataclass(frozen=True, slots=True, kw_only=True)
class Elicitation(ABC):
    """Base class of the elicitation request types.

    :param message: A human-readable explanation of why the input is needed.
    """

    message: str
    _mode: ClassVar[Literal["form", "url"]]

    @abstractmethod
    def _to_wire(self) -> ElicitRequestFormParams | ElicitRequestURLParams:
        """The ``elicitation/create`` request params."""


@dataclass(frozen=True, slots=True)
class FormElicitation(Elicitation):
    """Collect structured data from the user through the client.

    Form mode must not be used for secrets or credentials; use :class:`URLElicitation`
    or one of its alternate forms such as :class:`SecureFormElicitation`.

    :param fields: The form fields, keyed by property name.
    """

    fields: Mapping[str, FieldSchema]
    _mode: ClassVar[Literal["form"]] = "form"

    def __post_init__(self) -> None:
        if not self.fields:
            raise ValueError("fields must not be empty")

    def _to_wire(self) -> ElicitRequestFormParams:
        schema: _protocol._RequestedSchema = {
            "type": "object",
            "properties": {name: f._to_wire() for name, f in self.fields.items()},
        }
        if required := tuple(k for k, f in self.fields.items() if f.required):
            schema["required"] = required
        return {"mode": "form", "message": self.message, "requestedSchema": schema}


@dataclass(frozen=True, slots=True)
class URLElicitation(Elicitation):
    """Direct the user to a URL for an out-of-band interaction.

    Required for sensitive information (credentials, payment details etc.), which
    must never pass through the MCP client. The URL must not grant access
    by itself and must verify the user's identity.

    See :class:`SecureFormElicitation` for a simple ready-to-use solution.

    :param url: The URL the user should navigate to.
    """

    url: str
    _mode: ClassVar[Literal["url"]] = "url"

    def __post_init__(self) -> None:
        parsed = URL(self.url)
        if not parsed.is_absolute() or parsed.scheme not in {"http", "https"}:
            raise ValueError("url must be an absolute https URL")

    def _to_wire(self) -> ElicitRequestURLParams:
        return {"mode": "url", "message": self.message, "url": self.url}


@dataclass(frozen=True, slots=True)
class SecureFormElicitation(Elicitation):
    """Collect sensitive data through a form page served by this library.

    Use this as a simplified URL elicitation, where the library handles
    all the endpoint details.

    Requires authorization (the values are bound to the caller's subject)
    and a way to identify the browser user: automatic when
    :class:`.LocalAuthorization` is in use, otherwise pass ``authenticate_request``
    to :func:`~aiohttp_mcp.setup`. Pending submissions are held in memory,
    so multi-worker deployments need sticky routing to the worker that
    issued the link.

    :param fields: The form fields, keyed by property name.
    """

    fields: Mapping[str, FieldSchema]
    _mode: ClassVar[Literal["url"]] = "url"

    def __post_init__(self) -> None:
        if not self.fields:
            raise ValueError("fields must not be empty")

    def _to_wire(self) -> ElicitRequestURLParams:
        # The URL exists only once the server has minted a ticket; the
        # result builder calls _to_wire_url() instead.
        raise RuntimeError("secure forms are built by the server")

    def _to_wire_url(self, url: str) -> ElicitRequestURLParams:
        return {"mode": "url", "message": self.message, "url": url}


@dataclass(slots=True)
class _PendingForm:
    """A minted secure-form ticket awaiting submission in the browser."""

    subject: str
    elicitation: SecureFormElicitation
    expires: float
    values: Mapping[str, object] | None = None
    submitted: asyncio.Event = field(default_factory=asyncio.Event)


def _prune_forms(server: "MCPServer") -> None:
    """Drop expired pending forms; consumed tickets pop as stale entries."""
    now = time.time()
    while server._form_expiries and server._form_expiries[0][0] <= now:
        _, ticket = heapq.heappop(server._form_expiries)
        server._forms.pop(ticket, None)


@dataclass(frozen=True, slots=True)
class ElicitResult:
    """The user's response to an elicitation.

    :param action: ``"accept"`` (user submitted), ``"decline"`` (explicitly
        refused), or ``"cancel"`` (dismissed without choosing).
    :param content: The submitted form data, validated against the requested
        fields. ``None`` unless if ``action`` is not ``"accept"``.
    """

    action: Literal["accept", "decline", "cancel"]
    content: Mapping[str, object] | None = None


class InputRequired(Exception):
    """Terminate the current attempt, asking the client for user input.

    Raised by :meth:`~aiohttp_mcp.MCPRequest.elicit`, or directly by a
    handler to batch several requests into one round trip or, with no
    new requests at all, to signal that an out-of-band interaction has not
    completed yet, allowing the client to retry.

    :param elicitations: The requests for the client, keyed by the names used
        to retrieve their responses on the retry.
    :param state: Application state echoed back by the client. It is
        HMAC-signed, bound to the caller and request, and available as
        :attr:`~aiohttp_mcp.MCPRequest.state` on the retry. Must be JSON-serializable.
    """

    elicitations: Mapping[str, Elicitation]
    state: Mapping[str, object] | None

    def __init__(
        self,
        elicitations: Mapping[str, Elicitation] | None = None,
        *,
        state: Mapping[str, object] | None = None,
    ) -> None:
        self.elicitations = elicitations or {}
        self.state = state
        super().__init__(f"Input required: {', '.join(self.elicitations) or 'retry'}")


def _parse_result(key: str, response: object, elicitation: Elicitation) -> ElicitResult:
    if (
        not isinstance(response, Mapping)
        or (action := response.get("action")) not in {"accept", "decline", "cancel"}
    ):
        raise MCPError(INVALID_PARAMS, f"Invalid elicitation response for {key!r}")
    assert isinstance(action, str)
    if action != "accept" or not isinstance(elicitation, FormElicitation):
        return ElicitResult(action)  # type: ignore[arg-type]

    content = response.get("content")
    if not isinstance(content, Mapping):
        raise MCPError(INVALID_PARAMS, f"Missing content for elicitation {key!r}")
    validated: dict[str, object] = {}
    for name, spec in elicitation.fields.items():
        if name not in content:
            if spec.required:
                raise MCPError(
                    INVALID_PARAMS, f"Missing field {name!r} in elicitation {key!r}"
                )
            continue
        try:
            validated[name] = spec._validate(content[name])
        except ValueError as exc:
            raise MCPError(
                INVALID_PARAMS,
                f"Invalid field {name!r} in elicitation {key!r}: {exc}",
            ) from None
    # Unrecognised content keys are dropped rather than rejected.
    return ElicitResult("accept", validated)


async def _secure_form_result(
    ctx: "MCPRequest",
    key: str,
    elicitation: SecureFormElicitation,
    response: object,
    state: Mapping[str, object] | None,
) -> ElicitResult:
    """Resolve a secure form from the server-side store rather than the client.

    The client's response only carries the action; accepted values live in
    the pending-form store until the browser submission arrives.
    """
    result = _parse_result(key, response, elicitation)
    server = ctx._server
    ticket = ctx._form_tickets.get(key)
    if result.action != "accept":
        if ticket is not None:
            server._forms.pop(ticket, None)
        return result
    _prune_forms(server)
    pending = None if ticket is None else server._forms.get(ticket)
    if pending is None:
        # Expired or already consumed: issue a fresh form.
        raise InputRequired({key: elicitation}, state=state)
    if pending.values is None:
        # Not submitted in the browser yet: wait briefly, then ask client to retry.
        try:
            await asyncio.wait_for(pending.submitted.wait(), _FORM_WAIT)
        except TimeoutError:
            raise InputRequired(state=state) from None
        assert pending.values is not None
    assert ticket is not None
    del server._forms[ticket]  # Single use
    return ElicitResult("accept", pending.values)


def _request_digest(ctx: "MCPRequest") -> str:
    """Identifies the originating request, to reject replay on another one."""
    canonical = json.dumps(
        {"method": ctx.method, "params": ctx.params},
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


def _load_request_state(signer: _Signer, ctx: "MCPRequest") -> None:
    """Verify an echoed ``requestState`` and merge the responses it carries.

    Rejection is mandatory for state failing integrity, principal, expiry, or
    originating-request verification (MRTR server requirements 4-5).
    """
    responses: dict[str, object] = {}
    if ctx._request_state is not None:
        envelope = signer.verify(ctx._request_state)
        expiry = None if envelope is None else envelope.get("exp")
        subject = None if ctx.auth is None else ctx.auth.subject
        if (
            envelope is None
            or not isinstance(expiry, (int, float))
            or expiry <= time.time()
            or envelope.get("sub") != subject
            or envelope.get("req") != _request_digest(ctx)
        ):
            raise MCPError(INVALID_PARAMS, "Invalid or expired request state")
        prior = envelope.get("r")
        if isinstance(prior, Mapping):
            responses.update(prior)
        tickets = envelope.get("f")
        if isinstance(tickets, Mapping):
            ctx._form_tickets = {k: t for k, t in tickets.items() if isinstance(t, str)}
        ctx.state = envelope.get("s")
    if ctx._input_responses is not None:
        responses.update(ctx._input_responses)
    ctx._responses = responses


def _check_capability(ctx: "MCPRequest", elicitation: Elicitation) -> None:
    capability = ctx.meta[META_CLIENT_CAPABILITIES].get("elicitation")
    # An empty capabilities object declares form-mode support only.
    # https://modelcontextprotocol.io/specification/2026-07-28/client/elicitation#capabilities
    supported = None if capability is None else (set(capability) or {"form"})
    if supported is None or elicitation._mode not in supported:
        raise MCPError(
            MISSING_CLIENT_CAPABILITY,
            f"Client does not support {elicitation._mode}-mode elicitation",
            data={"requiredCapabilities": {"elicitation": {elicitation._mode: {}}}},
        )


def _input_required_result(
    ctx: "MCPRequest", supported: bool, exc: InputRequired
) -> InputRequiredResult:
    """Build the ``input_required`` result for a raised ``InputRequired``."""
    if not supported:
        # https://modelcontextprotocol.io/specification/2026-07-28/basic/patterns/mrtr#supported-requests
        logger.error(
            "InputRequired raised by %r, which does not support multi round-trip requests",
            ctx.method,
        )
        raise MCPError(INTERNAL_ERROR, "Internal error") from None
    for elicitation in exc.elicitations.values():
        _check_capability(ctx, elicitation)

    envelope: dict[str, object] = {
        "exp": time.time() + _STATE_TTL,
        "sub": None if ctx.auth is None else ctx.auth.subject,
        "req": _request_digest(ctx),
        # Responses should accumulate over multiple requests.
        "r": ctx._responses,
    }
    if exc.state is not None:
        envelope["s"] = exc.state

    server = ctx._server
    requests: dict[str, ElicitRequest] = {}
    tickets = dict(ctx._form_tickets)
    for key, elicitation in exc.elicitations.items():
        if isinstance(elicitation, SecureFormElicitation):
            requests[key] = {
                "method": "elicitation/create",
                "params": elicitation._to_wire_url(
                    _mint_form(server, ctx, tickets, key, elicitation)
                ),
            }
        else:
            requests[key] = {"method": "elicitation/create", "params": elicitation._to_wire()}
    if tickets:
        # Carried across round trips so a retry finds the same form.
        envelope["f"] = tickets
    result: InputRequiredResult = {
        "resultType": "input_required",
        "requestState": server._state_signer.sign(envelope),
    }
    if requests:
        result["inputRequests"] = requests
    return result


def _mint_form(
    server: "MCPServer",
    ctx: "MCPRequest",
    tickets: dict[str, str],
    key: str,
    elicitation: SecureFormElicitation,
) -> str:
    """The browser URL for a secure form, minting a ticket if needed."""
    subject = None if ctx.auth is None else ctx.auth.subject
    if subject is None or server._form_resource is None:
        logger.error(
            "Secure form elicitation requires authorization with a subject and"
            " a browser identity (LocalAuthorization or authenticate_request)"
        )
        raise MCPError(INTERNAL_ERROR, "Internal error") from None
    if not server.host_allowed(ctx.http.url.host):
        raise MCPError(
            INVALID_REQUEST,
            f"Unrecognised host for this server: {ctx.http.url.host!r}",
        )

    forms = server._forms
    _prune_forms(server)
    ticket = tickets.get(key)
    if ticket is None or ticket not in forms:
        while len(forms) >= _MAX_PENDING_FORMS:
            _, evict = heapq.heappop(server._form_expiries)
            forms.pop(evict, None)
        ticket = secrets.token_urlsafe()
        expires = time.time() + _STATE_TTL
        forms[ticket] = _PendingForm(subject, elicitation, expires)
        heapq.heappush(server._form_expiries, (expires, ticket))
        tickets[key] = ticket
    return str(ctx.http.url.origin().join(server._form_resource.url_for(ticket=ticket)))
