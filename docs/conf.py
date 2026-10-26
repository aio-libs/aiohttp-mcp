import sys
import tomllib
from pathlib import Path

from docutils import nodes

_REPO_ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(_REPO_ROOT))

# -- Project information -----------------------------------------------------

_PYPROJECT = _REPO_ROOT / "pyproject.toml"
# Parse the version from pyproject.toml rather than importing the package.
release = tomllib.loads(_PYPROJECT.read_text("utf-8"))["project"]["version"]
version = ".".join(release.split(".")[:2])

project = "aiohttp-mcp"
copyright = "2026, aio-libs contributors"
author = "aio-libs contributors"

github_url = "https://github.com"
github_repo_org = "aio-libs"
github_repo_name = "aiohttp-mcp"
github_repo_slug = f"{github_repo_org}/{github_repo_name}"
github_repo_url = f"{github_url}/{github_repo_slug}"

# -- General configuration ---------------------------------------------------

extensions = [
    "sphinx.ext.autodoc",
    "sphinx.ext.napoleon",
    "sphinx.ext.extlinks",
    "sphinx.ext.intersphinx",
    "sphinx.ext.viewcode",
]

try:
    import sphinxcontrib.spelling  # noqa: F401

    extensions.append("sphinxcontrib.spelling")
except ImportError:
    # Spelling is optional locally (e.g. unavailable in Windows), checked in CI.
    pass

source_suffix = ".rst"
master_doc = "index"
exclude_patterns = ["_build"]

# The default language to highlight source code in.
highlight_language = "python3"

# -- Options for autodoc -----------------------------------------------------

# The API reference is generated from docstrings, with type annotations rendered
# in the signatures. Every annotation becomes a cross-reference resolved under
# ``nitpicky``; aiohttp-mcp types are documented in api.rst, external ones via
# intersphinx or ``nitpick_ignore``.
autodoc_member_order = "bysource"
autodoc_typehints = "signature"
autoclass_content = "class"
add_module_names = False
autodoc_default_options = {
    "members": True,
    "undoc-members": True,
    "show-inheritance": True,
}

# -- Options for intersphinx -------------------------------------------------

intersphinx_mapping = {
    "python": ("https://docs.python.org/3", None),
    # TODO(aiohttp4): Switch to /stable/
    "aiohttp": ("https://docs.aiohttp.org/en/latest/", None),
    "aiohttp-security": ("https://aiohttp-security.readthedocs.io/en/stable/", None),
    "aiohttp-session": ("https://aiohttp-session.readthedocs.io/en/stable/", None),
    "aiohttp-remotes": ("https://aiohttp-remotes.readthedocs.io/en/stable/", None),
}

# -- Options for extlinks ----------------------------------------------------

extlinks = {
    "issue": (f"{github_repo_url}/issues/%s", "#%s"),
    "pr": (f"{github_repo_url}/pull/%s", "PR #%s"),
    "commit": (f"{github_repo_url}/commit/%s", "%s"),
    "gh": (f"{github_url}/%s", "GitHub: %s"),
    "user": (f"{github_url}/%s", "@%s"),
}

# -- Options for HTML output -------------------------------------------------

html_theme = "alabaster"

# -- Options for the spelling builder ----------------------------------------

spelling_warning = True
# Acronyms (MCP, HMAC, PKCE) and CamelCase names (OAuth-adjacent product names)
# are skipped automatically. Code entities use their Python-domain roles (:mod:,
# :func:, :class:), abbreviations/programs use :abbr:/:program:, and product names
# use the custom :brand: role (registered in setup() below) — all skipped by the
# checker — so the wordlist only holds genuine prose vocabulary.
spelling_ignore_acronyms = True
spelling_ignore_wiki_words = True

# -- Nitpicky mode -----------------------------------------------------------

nitpicky = True
# https://github.com/sphinx-doc/sphinx/issues/14462
nitpick_ignore = [
    ("py:class", "aiohttp_mcp._tools._T"),
    ("py:obj", "aiohttp_mcp._tools._T"),
]

def _resolve_type_alias(app, env, node, contnode):
    # TODO: Drop this function when fixed: https://github.com/sphinx-doc/sphinx/issues/14745
    module, _, name = node["reftarget"].rpartition(".")
    if not module.startswith("aiohttp_mcp._"):
        return None
    node["reftarget"] = target = f"aiohttp_mcp.{name}"
    return env.domains["py"].resolve_xref(
        env, node.get("refdoc"), app.builder, "obj", target, node, contnode
    )


def setup(app):
    """Register a ``:brand:`` role for product names that have no built-in role.

    The role wraps its text in an inline node — a ``<span class="brand">`` — so it
    renders as ordinary prose, can be styled via CSS, and is skipped by the spelling
    checker.
    """

    def brand(name, rawtext, text, lineno, inliner, options=None, content=None):
        # A structural ``inline`` node (rather than tagging a Text node) survives
        # doctree pickling — CI builds html before spelling — and carries a
        # ``brand`` class for styling.
        return [nodes.inline(rawtext, text, classes=["brand"])], []

    app.add_role("brand", brand)
    app.connect("missing-reference", _resolve_type_alias)
