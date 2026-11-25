from collections.abc import Mapping

from fastapi import FastAPI

from fledge.core import Trigger
from fledge.settings import Settings
from fledge.store import Store
from fledge.web.internal_api import internal_routes
from fledge.web.pages import page_routes
from fledge.web.public_api import public_routes


def create_app(
    store: Store, triggers: Mapping[str, Trigger], config: Settings
) -> FastAPI:
    workflows = {trigger.name: trigger.workflow for trigger in config.triggers}
    if set(workflows) != set(triggers):
        raise ValueError(
            f"triggers {sorted(triggers)} do not match the configured "
            f"triggers {sorted(workflows)}"
        )
    app = FastAPI(title="fledge")
    app.include_router(public_routes(store, triggers, workflows))
    app.include_router(internal_routes(store))
    app.include_router(page_routes(store, sorted(triggers)))
    return app
