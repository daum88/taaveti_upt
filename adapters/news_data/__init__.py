"""External news-evidence transport adapters.

Each module here is a small true-external port over a single free news source.
Callers depend on a narrow ``fetch`` surface that returns clean records and
never see the HTTP request, feed payload, or timestamp-parsing mechanics.
Transport and payload-parse failures surface as :class:`NewsSourceError`.
"""

from adapters.news_data.errors import NewsSourceError

__all__ = ["NewsSourceError"]
