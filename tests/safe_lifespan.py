"""Route tests with the real app and HTTP pool, without starting hardware I/O."""
from contextlib import asynccontextmanager

import httpx


def create_test_app():
    from engine.app import create_app
    from engine.core.runner_service import broker

    app = create_app()

    @asynccontextmanager
    async def lifespan(application):
        import asyncio

        broker.bind_loop(asyncio.get_running_loop())
        async with httpx.AsyncClient(follow_redirects=False) as client:
            application.state.http = client
            try:
                yield
            finally:
                await application.state.launch_requests.close()

    app.router.lifespan_context = lifespan
    return app
