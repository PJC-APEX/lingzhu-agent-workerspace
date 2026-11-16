"""Rollback failed synchronous use cases without moving commit boundaries."""
from functools import wraps


def rollback_on_error(function):
    @wraps(function)
    def invoke(db, *args, **kwargs):
        try:
            return function(db, *args, **kwargs)
        except Exception:
            db.rollback()
            raise
    return invoke
