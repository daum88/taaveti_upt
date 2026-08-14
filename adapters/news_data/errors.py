"""Typed external errors for the news-transport ports.

Each news-source adapter owns its transport and payload-parsing failures and
surfaces them as a single ``NewsSourceError``.  Callers (e.g.
:mod:`services.news_research`) isolate a failing source without importing
``requests`` or XML parsing internals or depending on any concrete provider
library.
"""

from __future__ import annotations


class NewsSourceError(Exception):
    """A news-source transport or payload-parsing failure.

    Adapters raise this instead of leaking ``requests`` or parser exceptions so
    the retrieval seam stays free of concrete transport dependencies.
    """
