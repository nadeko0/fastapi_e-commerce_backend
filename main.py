import sys

import uvicorn

from app.core.config import settings
from app.core.http_protocol import NoDelayH11Protocol

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
        # h11 with TCP_NODELAY on each connection: with workers > 1 uvicorn
        # 0.54's shared listener left Nagle on, +40 ms per keep-alive
        # request (app/core/http_protocol.py).
        http=NoDelayH11Protocol,
        # The application writes one JSON access line per request
        # (app/core/access_log.py); uvicorn's plain-text one would repeat it.
        access_log=False,
        # No "Server: uvicorn" on responses: it names the stack for nothing.
        server_header=False,
        # On SIGTERM, wait this long for requests in flight, then cancel them.
        # The longest wait is a Stripe call: (5 s connect + 20 s read) x 3
        # tries + 1.5 s SDK backoff = 76.5 s with the default STRIPE_*
        # settings (app/core/config.py). Keep it below the api service's
        # stop_grace_period in docker-compose.yml, or Docker sends SIGKILL.
        timeout_graceful_shutdown=90,
    )
