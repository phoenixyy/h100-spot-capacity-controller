"""Deployment entry points with one structured result log per invocation."""

import json
import logging

from h100_spot_controller import handlers

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)


def _invoke(name, event, context):
    try:
        result = getattr(handlers, name)(event, context)
        logger.info(json.dumps({"handler": name, "result": result}, sort_keys=True, default=str))
        return result
    except Exception:
        logger.exception(json.dumps({"handler": name, "status": "unhandled_error"}))
        raise


def reconcile(event, context):
    return _invoke("reconcile", event, context)


def collect(event, context):
    return _invoke("collect", event, context)


def spot_event(event, context):
    return _invoke("spot_event", event, context)

__all__ = ["collect", "reconcile", "spot_event"]
