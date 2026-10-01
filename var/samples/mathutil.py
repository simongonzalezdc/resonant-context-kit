"""Synthetic math utilities — sandbox fixture for contextkit.munch.read_symbol.

Synthetic arithmetic helpers (no real data). Used to verify symbol spans,
class reads, and the mtime-validated read cache through the service surface.
"""

DEFAULT_SCALE = 2


def clamp(value, lo, hi):
    """Clamp value into the closed interval [lo, hi]."""
    if value < lo:
        return lo
    if value > hi:
        return hi
    return value


def scaled(value, scale=DEFAULT_SCALE):
    """Multiply value by scale, floored to an int."""
    return int(value * scale)


class RunningTotal:
    """A tiny accumulator with a reset."""

    def __init__(self, start=0):
        self.total = start
        self.events = 0

    def add(self, amount):
        """Add one amount and return the running total."""
        self.total += amount
        self.events += 1
        return self.total

    def reset(self, start=0):
        """Reset the accumulator to start."""
        self.total = start
        self.events = 0
        return self.total
