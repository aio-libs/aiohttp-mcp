import os
import secrets
from collections.abc import Awaitable, Callable, Sequence

import aiohttp_mcp
from aiohttp import ClientSession, web
from yarl import URL

delete_project: aiohttp_mcp.PromptHandler
exchange_code: Callable[[str], Awaitable[str]]
fetch_events: Callable[[str], Awaitable[Sequence[object]]]
is_signed: Callable[[str, str], Awaitable[bool]]
store_key: Callable[[str, str], Awaitable[None]]


async def form_elicitation(request: aiohttp_mcp.MCPRequest, date: str) -> str:
    form = aiohttp_mcp.FormElicitation(
        {
            "seat": aiohttp_mcp.StringSchema(
                description="Preferred seat", min_length=2, max_length=3
            ),
            "meal": aiohttp_mcp.EnumSchema(
                options={"veg": "Vegetarian", "std": "Standard"},
                default="std",
                required=False,
            ),
        },
        message=f"Book the flight on {date}?",
    )

    confirm = await request.elicit("confirm", form)
    if confirm.action != "accept":
        return "The booking was not confirmed, so nothing was booked."
    assert confirm.content is not None  # Always present for accepted forms.
    return f"Booked flight on {date}, seat {confirm.content['seat']}."


async def url_elicitation(request: aiohttp_mcp.MCPRequest, document: str) -> str:
    assert request.auth is not None and request.auth.subject is not None
    if not await is_signed(document, request.auth.subject):
        grant = await request.elicit(
            "signature",
            aiohttp_mcp.URLElicitation(
                "https://mcp.example.com/sign", message=f"Sign {document} to continue.",
            ),
        )
        if grant.action != "accept":
            return "The document was not signed."
        if not await is_signed(document, request.auth.subject):
            raise aiohttp_mcp.InputRequired(state={})
    return f"{document} signed."


async def secure_form_elicitation(request: aiohttp_mcp.MCPRequest) -> str:
    result = await request.elicit(
        "credentials",
        aiohttp_mcp.SecureFormElicitation(
            {"api_key": aiohttp_mcp.StringSchema(min_length=8)},
            message="Provide your weather API key.",
        ),
    )
    if result.action != "accept":
        return "No API key provided."
    assert result.content is not None  # Always present for accepted forms.
    assert request.auth is not None and request.auth.subject is not None
    await store_key(request.auth.subject, str(result.content["api_key"]))
    return "API key saved."


def same_site_endpoint(app: web.Application, authorization: aiohttp_mcp.Authorization) -> None:
    from aiohttp_security import authorized_userid

    # Pending approvals: opaque ID -> (requesting MCP subject, approved).
    approvals: dict[str, tuple[str, bool]] = {}


    async def delete_project(request: aiohttp_mcp.MCPRequest, project: str) -> str:
        """Delete a project, after out-of-band confirmation."""
        assert request.auth is not None  # auth is only None when auth not configured
        subject = request.auth.subject
        assert subject is not None

        if isinstance(request.state, dict):  # A retry of an earlier attempt.
            approval_id = request.state["approval"]
            assert isinstance(approval_id, str)
        else:
            approval_id = secrets.token_urlsafe()
        if approval_id not in approvals:
            # Record who is asking; only this user may approve.
            approvals[approval_id] = (subject, False)

        # Must validate the request was addressed to an allowed origin
        # before we use it to create a URL to direct the user to.
        if not server.host_allowed(request.http.url.host):
            raise web.HTTPBadRequest(text="Unrecognised host")
        approve_url = request.http.url.origin().join(
            request.http.app.router["approve"].url_for(id=approval_id)
        )
        grant = await request.elicit(
            "approve",
            aiohttp_mcp.URLElicitation(
                str(approve_url), message=f"Approve deletion of {project} to continue.",
            ),
            state={"approval": approval_id},
        )
        if grant.action != "accept":
            del approvals[approval_id]
            return "The deletion was not approved."
        if not approvals[approval_id][1]:
            # Consent to open the URL is not approval itself: the browser
            # interaction may still be in progress, so ask for a retry.
            raise aiohttp_mcp.InputRequired(state={"approval": approval_id})
        del approvals[approval_id]
        return f"{project} deleted."


    async def approve(request: web.Request) -> web.Response:
        user = await authorized_userid(request)
        if user is None:
            # The application's normal login, returning here afterwards.
            login = app.router["login"].url_for().with_query({"redirect": str(request.rel_url)})
            raise web.HTTPFound(login)
        record = approvals.get(request.match_info["id"])
        # The user approving in the browser must be the same user who
        # requested the deletion through the MCP client.
        if record is None or record[0] != user:
            raise web.HTTPForbidden()
        if request.method == "POST":
            approvals[request.match_info["id"]] = (record[0], True)
            return web.Response(text="Approved. You can return to your agent.")
        page = "<form method='post'><button>Approve deletion</button></form>"
        return web.Response(text=page, content_type="text/html")


    resource = app.router.add_resource("/approve/{id}", name="approve")
    resource.add_route("GET", approve)
    resource.add_route("POST", approve)
    server = aiohttp_mcp.setup(
        app,
        authorization=authorization,
        prompts=aiohttp_mcp.PromptRegistry(delete_project),
    )


def external_as_login(app: web.Application, authorization: aiohttp_mcp.Authorization) -> None:
    from aiohttp_session import get_session, new_session
    from aiohttp_session import setup as session_setup
    from aiohttp_session.cookie_storage import EncryptedCookieStorage

    AUTH_URL = URL("https://auth.example/authorize")
    TOKEN_URL = "https://auth.example/oauth/token"
    USERINFO_URL = "https://auth.example/userinfo"
    CLIENT_ID = "mcp-server-web"
    SESS = web.AppKey("SESS", ClientSession)

    # Pending approvals: opaque ID -> (requesting MCP subject, approved).
    approvals: dict[str, tuple[str, bool]] = {}

    async def browser_subject(request: web.Request) -> str | None:
        """Who the browser user is, from the login session cookie."""
        session = await get_session(request)
        sub: str | None = session.get("sub")
        return sub

    async def login(request: web.Request) -> web.Response:
        """Authenticate the browser against the auth server."""
        session = await new_session(request)  # A fresh session, for login CSRF.
        state = secrets.token_urlsafe()
        session["oidc"] = {"state": state, "next": request.query.get("redirect", "/")}

        # Must validate the request was addressed to an allowed origin
        # before we use it to create a URL to direct the user to.
        if not server.host_allowed(request.url.host):
            raise web.HTTPBadRequest(text="Unrecognised host")
        redirect_uri = request.url.origin().join(
            request.app.router["oidc-callback"].url_for()
        )
        raise web.HTTPFound(
            AUTH_URL.with_query(
                {
                    "response_type": "code",
                    "client_id": CLIENT_ID,
                    "scope": "openid",
                    "redirect_uri": str(redirect_uri),
                    "state": state,
                }
            )
        )

    async def oidc_callback(request: web.Request) -> web.Response:
        session = await get_session(request)
        flow = session.pop("oidc", None)
        if flow is None or request.query.get("state") != flow["state"]:
            raise web.HTTPForbidden()
        # Must validate the request was addressed to an allowed origin
        # before we use it to create a URL to direct the user to.
        if not server.host_allowed(request.url.host):
            raise web.HTTPBadRequest(text="Unrecognised host")
        redirect_uri = request.url.origin().join(
            request.app.router["oidc-callback"].url_for()
        )

        async with request.app[SESS].post(
            TOKEN_URL,
            data={
                "grant_type": "authorization_code",
                "code": request.query["code"],
                "redirect_uri": str(redirect_uri),
                "client_id": CLIENT_ID,
                "client_secret": os.environ["OIDC_CLIENT_SECRET"],
            },
        ) as resp:
            grant = await resp.json()

        # Ask the auth server who just logged in.
        async with request.app[SESS].get(
            USERINFO_URL, headers={"Authorization": f"Bearer {grant['access_token']}"},
        ) as resp:
            claims = await resp.json()
        session["sub"] = claims["sub"]

        target = URL(flow["next"])
        if target.is_absolute():  # Only redirect back within this application.
            raise web.HTTPForbidden()
        raise web.HTTPFound(target)

    async def approve(request: web.Request) -> web.Response:
        user = await browser_subject(request)
        if user is None:
            login_url = request.app.router["login"].url_for()
            raise web.HTTPFound(login_url.with_query({"redirect": str(request.rel_url)}))
        record = approvals.get(request.match_info["id"])
        # The browser user and the MCP caller are identified by the same
        # author server, so their subjects compare directly.
        if record is None or record[0] != user:
            raise web.HTTPForbidden()
        if request.method == "POST":
            approvals[request.match_info["id"]] = (record[0], True)
            return web.Response(text="Approved. You can return to your agent.")
        page = "<form method='post'><button>Approve deletion</button></form>"
        return web.Response(text=page, content_type="text/html")

    session_setup(app, EncryptedCookieStorage(bytes.fromhex(os.environ["SESSION_KEY"])))
    app.router.add_get("/login", login, name="login")
    app.router.add_get("/oidc/callback", oidc_callback, name="oidc-callback")
    resource = app.router.add_resource("/approve/{id}", name="approve")
    resource.add_route("GET", approve)
    resource.add_route("POST", approve)
    server = aiohttp_mcp.setup(
        app,
        authorization=authorization,
        prompts=aiohttp_mcp.PromptRegistry(delete_project),
    )


def oauth_elicitation(app: web.Application, authorization: aiohttp_mcp.Authorization) -> None:
    from aiohttp_security import authorized_userid

    # Third-party OAuth tokens, keyed by user ID.
    calendar_tokens: dict[str, str] = {}
    # In-flight OAuth flows: ``state`` parameter -> user ID.
    flow_states: dict[str, str] = {}

    async def import_calendar(request: aiohttp_mcp.MCPRequest) -> str:
        """Import the user's external calendar."""
        assert request.auth is not None and request.auth.subject is not None
        subject = request.auth.subject
        if subject not in calendar_tokens:

            # Must validate the request was addressed to an allowed origin
            # before we use it to create a URL to direct the user to.
            if not server.host_allowed(request.http.url.host):
                raise web.HTTPBadRequest(text="Unrecognised host")
            connect_url = request.http.url.origin().join(
                request.http.app.router["connect-calendar"].url_for()
            )
            grant = await request.elicit(
                "grant",
                aiohttp_mcp.URLElicitation(
                    # Our own endpoint, not the third party's authorization
                    # URL: it must verify who opened it before the flow starts.
                    str(connect_url),
                    message="Connect your calendar account to continue.",
                ),
            )
            if grant.action != "accept":
                return "Calendar not connected."
            if subject not in calendar_tokens:
                # The browser flow has not finished; ask the client to retry.
                raise aiohttp_mcp.InputRequired(state={"waiting": True})
        events = await fetch_events(calendar_tokens[subject])
        return f"Imported {len(events)} events."

    async def connect(request: web.Request) -> web.Response:
        user = await authorized_userid(request)
        if user is None:
            login = URL("/login").with_query({"redirect": str(request.rel_url)})
            raise web.HTTPFound(login)
        # Bind the flow to the logged-in browser user; the callback stores
        # tokens only for this identity, never for whoever started the elicitation.
        state = secrets.token_urlsafe()
        flow_states[state] = user

        # Must validate the request was addressed to an allowed origin
        # before we use it to create a URL to direct the user to.
        if not server.host_allowed(request.url.host):
            raise web.HTTPBadRequest(text="Unrecognised host")
        redirect_uri = request.url.origin().join(
            request.app.router["calendar-callback"].url_for()
        )
        raise web.HTTPFound(
            URL("https://calendar.example/oauth/authorize").with_query(
                {
                    "response_type": "code",
                    "client_id": "mcp-example",
                    "redirect_uri": str(redirect_uri),
                    "state": state,
                }
            )
        )

    async def callback(request: web.Request) -> web.Response:
        user = flow_states.pop(request.query.get("state", ""), None)
        if user is None or user != await authorized_userid(request):
            raise web.HTTPForbidden()
        calendar_tokens[user] = await exchange_code(request.query["code"])
        return web.Response(text="Calendar connected. You can return to your agent.")

    app.router.add_get("/connect/calendar", connect, name="connect-calendar")
    app.router.add_get("/connect/calendar/callback", callback, name="calendar-callback")
    server = aiohttp_mcp.setup(
        app,
        authorization=authorization,
        prompts=aiohttp_mcp.PromptRegistry(import_calendar),
    )
