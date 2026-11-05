"""The browser side of ``SecureFormElicitation``.

Serves the single-use form page a secure elicitation links to: verifies the
browser user is the one the form was minted for, renders the requested
fields as HTML inputs, and validates the submission against the field
schemas before storing it for the handler's next attempt.
"""

import html
import sys
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

if sys.version_info >= (3, 12):
    from typing import TypeAliasType
else:
    from typing_extensions import TypeAliasType

from aiohttp import web
from multidict import MultiDictProxy

from ._elicitation import (
    BooleanSchema,
    EnumSchema,
    FieldSchema,
    IntegerSchema,
    MultiSelectEnumSchema,
    NumberSchema,
    SecureFormElicitation,
    StringSchema,
    _PendingForm,
    _prune_forms,
)

if TYPE_CHECKING:
    from ._server import MCPServer

_PAGE_TEMPLATE: Final = """<!DOCTYPE html>
<html>
<head><meta charset="utf-8"><title>{server}</title></head>
<body>
<h1>{server}</h1>
<p>{message}</p>
{error}<form method="post">
{fields}<button>Submit</button>
</form>
</body>
</html>"""

_STRING_INPUT_TYPES: Final = {
    "email": "email",
    "uri": "url",
    "date": "date",
    "date-time": "datetime-local",
}


class _InvalidSubmission(Exception):
    """A submitted form value failed validation; shown to the user."""


def _options_html(field: EnumSchema | MultiSelectEnumSchema) -> str:
    options = field.options
    titles = options if isinstance(options, dict) else {o: o for o in options}
    return "".join(
        f'<option value="{html.escape(value, quote=True)}">{html.escape(title)}</option>'
        for value, title in titles.items()
    )


def _field_html(name: str, field: FieldSchema) -> str:
    """One labelled form control for ``field``."""
    escaped = html.escape(name, quote=True)
    required = ' required="required"' if field.required else ""
    match field:
        case StringSchema():
            input_type = _STRING_INPUT_TYPES.get(field.format or "", "text")
            attrs = f'type="{input_type}"{required}'
            if field.min_length is not None:
                attrs += f' minlength="{field.min_length}"'
            if field.max_length is not None:
                attrs += f' maxlength="{field.max_length}"'
            if field.default is not None:
                attrs += f' value="{html.escape(field.default, quote=True)}"'
            control = f'<input name="{escaped}" {attrs}>'
        case IntegerSchema() | NumberSchema():
            step = "1" if isinstance(field, IntegerSchema) else "any"
            attrs = f'type="number" step="{step}"{required}'
            if field.minimum is not None:
                attrs += f' min="{field.minimum}"'
            if field.maximum is not None:
                attrs += f' max="{field.maximum}"'
            if field.default is not None:
                attrs += f' value="{field.default}"'
            control = f'<input name="{escaped}" {attrs}>'
        case BooleanSchema():
            checked = " checked" if field.default else ""
            control = f'<input name="{escaped}" type="checkbox"{checked}>'
        case EnumSchema():
            control = f'<select name="{escaped}"{required}>{_options_html(field)}</select>'
        case MultiSelectEnumSchema():
            control = f'<select name="{escaped}" multiple>{_options_html(field)}</select>'
        case _:
            assert False
    label = html.escape(field.title or name)
    description = f" - {html.escape(field.description)}" if field.description else ""
    return f"<label>{label}{description}<br />{control}</label><br />\n"


def _submitted_value(
    name: str, field: FieldSchema, form: "MultiDictProxy[str | bytes | bytearray | web.FileField]"
) -> object | None:
    """The submitted value for ``field``, coerced to its Python type."""
    if isinstance(field, BooleanSchema):
        return name in form
    if isinstance(field, MultiSelectEnumSchema):
        values = [v for v in form.getall(name, []) if isinstance(v, str)]
        return values or None
    raw = form.get(name)
    if not isinstance(raw, str) or not raw:
        return None
    if isinstance(field, (IntegerSchema, NumberSchema)):
        try:
            return int(raw) if isinstance(field, IntegerSchema) else float(raw)
        except ValueError:
            raise _InvalidSubmission(f"{name}: expected a number") from None
    return raw


@dataclass(frozen=True, slots=True)
class FormContext:
    """Everything a secure-form page needs to render.

    A custom page MUST render an HTML form that POSTs back to the same URL
    with one control per requested field, named exactly as in the
    elicitation's ``fields`` (ready-made controls: :attr:`fields_html`), and
    SHOULD display the elicitation's ``message``.
    """

    # The MCP server's display name.
    server_name: str
    # The elicitation being fulfilled.
    elicitation: SecureFormElicitation
    # Set when re-prompting after a rejected submission.
    error: str | None

    @property
    def fields_html(self) -> str:
        """The requested fields as escaped form controls with labels."""
        return "".join(_field_html(k, f) for k, f in self.elicitation.fields.items())


# TODO(PY312): use the `type` statement.
RenderForm = TypeAliasType(
    "RenderForm", Callable[[web.Request, "FormContext"], Awaitable[web.Response]]
)


async def render_form_default(request: web.Request, context: FormContext) -> web.Response:
    """The built-in secure-form page."""
    body = _PAGE_TEMPLATE.format(
        server=html.escape(context.server_name),
        message=html.escape(context.elicitation.message),
        error=f"<p><b>{html.escape(context.error)}</b></p>\n" if context.error else "",
        fields=context.fields_html,
    )
    return web.Response(text=body, content_type="text/html")


async def _render(
    server: "MCPServer", request: web.Request, pending: _PendingForm, error: str | None
) -> web.Response:
    context = FormContext(
        server_name=server._server_info["name"],
        elicitation=pending.elicitation,
        error=error,
    )
    return await server._form_render(request, context)


async def handle_form(server: "MCPServer", request: web.Request) -> web.StreamResponse:
    """GET renders the form; POST validates and stores the submission."""
    _prune_forms(server)
    pending = server._forms.get(request.match_info["ticket"])
    if pending is None:
        raise web.HTTPNotFound(text="This link has expired.")

    assert server._form_identity is not None  # Routes exist only when set.
    subject = await server._form_identity(request)
    if subject is None:
        if server._form_login is not None:
            server._form_login(request)
        raise web.HTTPUnauthorized(
            text="Sign in to the application first, then reopen this link."
        )
    # The user submitting in the browser must be the one for whom the
    # elicitation was created; the link itself grants nothing.
    if subject != pending.subject:
        raise web.HTTPForbidden()
    if pending.values is not None:
        return web.Response(text="Already submitted. You can return to your agent.")
    if request.method == "GET":
        return await _render(server, request, pending, None)

    form = await request.post()
    validated: dict[str, object] = {}
    try:
        for name, field in pending.elicitation.fields.items():
            value = _submitted_value(name, field, form)
            if value is None:
                if field.required:
                    raise _InvalidSubmission(f"{name} is required")
                continue
            try:
                validated[name] = field._validate(value)
            except ValueError as exc:
                raise _InvalidSubmission(f"{name}: {exc}") from None
    except _InvalidSubmission as exc:
        return await _render(server, request, pending, str(exc))
    pending.values = validated
    pending.submitted.set()  # Release a retry waiting for this form.
    return web.Response(text="Submitted. You can return to your agent.")
