import sys

import uvicorn

from app.core.config import settings

if __name__ == "__main__":
    uvicorn.run(
        "app.main:app",
        host=settings.HOST,
        port=settings.PORT,
        reload=settings.DEBUG,
        workers=settings.WORKERS,
        # psycopg's async mode (app.core.database.async_engine) cannot run on
        # Windows' ProactorEventLoop, which is what uvicorn picks there for a
        # single in-process worker. Linux/macOS already get a selector loop.
        loop="asyncio:SelectorEventLoop" if sys.platform == "win32" else "auto",
        # X-Forwarded-For is the application's business, by TRUSTED_PROXIES
        # (app/core/forwarded.py). uvicorn's own handling would rewrite the
        # client address first, by a list of its own.
        proxy_headers=False,
    )
