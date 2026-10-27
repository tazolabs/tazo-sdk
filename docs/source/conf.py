"""Sphinx emits the public SDK reference consumed by Mintlify."""

from tazo import __version__

project = "Tazo Python SDK"
author = "Tazo Labs"
version = release = __version__
extensions = ["sphinx.ext.autodoc", "sphinx.ext.napoleon", "sphinxcontrib.serializinghtml"]
autodoc_typehints = "description"
autodoc_member_order = "bysource"
html_theme = "alabaster"
html_last_updated_fmt = None
exclude_patterns = []


def resource_signature(app, what, name, obj, options, signature, return_annotation):
    # Resource objects are provided by clients, not constructed by customers.
    if what == "class" and name.startswith("tazo.resources."):
        return "", ""
    return None


def setup(app):
    app.connect("autodoc-process-signature", resource_signature)
