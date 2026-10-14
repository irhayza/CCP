"""Entry point ASGI: `uvicorn backend.asgi:app`."""
from .main import create_app

app = create_app()
