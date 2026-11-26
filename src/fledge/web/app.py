from collections.abc import Mapping

from fastapi import FastAPI

from fledge.store import Store
from fledge.web.internal_api import internal_routes
from fledge.web.pages import page_routes
from fledge.web.public_api import public_routes
from fledge.workflows import TriggerBinding


def create_app(store: Store, workflows: Mapping[str, TriggerBinding]) -> FastAPI:
    app = FastAPI(title="fledge")
    app.include_router(public_routes(store, workflows))
    app.include_router(internal_routes(store))
    app.include_router(page_routes(store, sorted(workflows)))
    return app
