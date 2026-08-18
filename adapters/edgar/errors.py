"""Errors raised by SEC EDGAR external ports."""


class EdgarSourceError(Exception):
    """An EDGAR request or payload failed; callers degrade without the source."""
