"""Entry point of the carnet plugin."""
from .provider import CarnetProvider


def register(ctx) -> None:
    ctx.register_memory_provider(CarnetProvider())
