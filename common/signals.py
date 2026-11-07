"""Signals between steps. A signal says WHAT is ready. MongoDB holds WHAT IT IS.

Local/dev: `dispatch()` calls the registered handler in-process.
Agentverse: the orchestrator uAgent calls the same handlers. Later, vendor
and locker agents can receive these as uAgent messages instead.
"""
from typing import Callable, Optional

from common.contracts import Signal

_handlers: dict[str, Callable[[Signal], Optional[Signal]]] = {}


def on(signal_type: str):
    def deco(fn):
        _handlers[signal_type] = fn
        return fn
    return deco


def dispatch(signal: Signal) -> Optional[Signal]:
    handler = _handlers.get(signal.type)
    if handler is None:
        raise RuntimeError(f"No handler registered for signal '{signal.type}'")
    return handler(signal)


def log(signal: Signal) -> str:
    extra = f" v{signal.version}" if signal.version else ""
    return f"⚡ {signal.type}{extra} [{signal.trip_id}]"
