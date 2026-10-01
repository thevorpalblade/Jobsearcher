"""Local web UI (FastAPI + Jinja + HTMX), started by `jobsearcher web`."""

from jobsearcher.web.app import create_app, create_app_from_env

__all__ = ["create_app", "create_app_from_env"]
