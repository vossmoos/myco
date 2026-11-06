"""Container entrypoint: ``python -m myco_mcp``. Configured only by environment variables."""

from __future__ import annotations

import logging
import sys

import uvicorn

from myco_mcp.config import ConfigError
from myco_mcp.connector import PackError
from myco_mcp.server import create_app
from myco_mcp.settings import Settings, SettingsError


def main() -> None:
    try:
        settings = Settings.from_env()
        logging.basicConfig(
            level=settings.log_level,
            format="%(asctime)s %(levelname)s %(name)s: %(message)s",
            stream=sys.stderr,
        )
        # httpx logs full request URLs at INFO; query strings can carry personal data.
        logging.getLogger("httpx").setLevel(logging.WARNING)
        app = create_app(settings)
    except (SettingsError, PackError, ConfigError) as exc:
        print(f"myco: {exc}", file=sys.stderr)
        sys.exit(2)
    uvicorn.run(
        app,
        host=settings.host,
        port=settings.port,
        log_level=settings.log_level.lower(),
        proxy_headers=True,
        forwarded_allow_ips="*",
    )


if __name__ == "__main__":
    main()
