"""Validated instrument-catalogue mutations for presentation adapters."""

from collections.abc import Callable
from dataclasses import asdict, dataclass
from functools import partial
from typing import Literal

from services.instrument_universe import (
    InstrumentValidationError,
    import_etf_catalogue,
    set_active,
    upsert_instrument,
)
from settings import Settings, load_settings


class InstrumentCommandError(Exception):
    """A requested catalogue mutation could not be completed."""


class InstrumentNotFound(InstrumentCommandError):
    """The requested catalogue entry does not exist."""


@dataclass(frozen=True)
class InstrumentDefinition:
    ticker: str
    instrument_type: Literal["equity", "etf"]
    company_name: str | None = None
    sector: str | None = None
    exchange: str | None = None
    issuer: str | None = None
    category: str | None = None
    is_active: bool = True


type CatalogueMutation = dict[str, object]
InstrumentWriter = Callable[..., CatalogueMutation]
InstrumentActivator = Callable[[str, bool], CatalogueMutation]
CatalogueImporter = Callable[..., CatalogueMutation]


class InstrumentCommands:
    """Hide provider validation and persistence behind three catalogue commands."""

    def __init__(
        self,
        *,
        writer: InstrumentWriter | None = None,
        activator: InstrumentActivator = set_active,
        importer: CatalogueImporter = import_etf_catalogue,
        etf_universe_enabled: bool | None = None,
        settings: Settings | None = None,
    ) -> None:
        self._settings = settings or load_settings()
        self._writer = writer or partial(upsert_instrument, settings=self._settings)
        self._activator = activator
        self._importer = importer
        self._etf_universe_enabled = (
            self._settings.etf_universe_enabled if etf_universe_enabled is None else etf_universe_enabled
        )

    def add(self, definition: InstrumentDefinition) -> CatalogueMutation:
        try:
            return self._writer(**asdict(definition))
        except InstrumentValidationError as error:
            raise InstrumentCommandError(str(error)) from error

    def set_active(self, ticker: str, is_active: bool) -> CatalogueMutation:
        try:
            return self._activator(ticker, is_active)
        except InstrumentValidationError as error:
            raise InstrumentNotFound(str(error)) from error

    def import_etfs(self, *, dry_run: bool = False) -> CatalogueMutation:
        return self._importer(active=self._etf_universe_enabled, dry_run=dry_run)
