"""Common cancellation checkpoint for background review reads."""
from concurrent.futures import CancelledError


def check_cancelled(cancelled):
    if cancelled is not None and cancelled():
        raise CancelledError
