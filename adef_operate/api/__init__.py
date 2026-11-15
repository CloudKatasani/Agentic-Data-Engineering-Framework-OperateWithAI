"""HTTP API (OPERATE_SPEC section 11) and the service layer behind it."""

from .fleet import Fleet

__all__ = ["Fleet", "create_app"]


def create_app(home=None):
    from .app import create_app as _create
    return _create(home)
