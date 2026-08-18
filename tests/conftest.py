"""Session-wide pytest setup.

``settings.load_settings`` loads the project ``.env`` into ``os.environ`` whenever
``config`` is imported, so assertions about secure defaults (loopback binding,
operator access control) would otherwise depend on the developer's local machine
configuration. Tests must be deterministic: neutralize dotenv before any test
module imports ``config``. CI has no ``.env``; this makes local runs match it.
"""

import settings


def _skip_local_env_file(*_args, **_kwargs):
    return None


settings.load_dotenv = _skip_local_env_file
