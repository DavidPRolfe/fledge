from collections.abc import Mapping

from fastapi import FastAPI

from fledge.control.snapshots import TriggerBinding
from fledge.control.store import Store
from fledge.control.web.internal_api import internal_routes
from fledge.control.web.pages import page_routes
from fledge.control.web.public_api import public_routes


def create_app(store: Store, workflows: Mapping[str, TriggerBinding]) -> FastAPI:
    app = FastAPI(title="fledge")
    app.include_router(public_routes(store, workflows))
    app.include_router(internal_routes(store))
    app.include_router(page_routes(store, sorted(workflows)))
    return app
