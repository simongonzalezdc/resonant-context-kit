"""Synthetic greeter module — sandbox fixture for contextkit.munch.read_symbol.

Content is synthetic (no real names, paths, or secrets). This file exists so a
client can exercise symbol-level reads: reading one function here instead of
the whole file is the kit's measured -98% tool-bytes move (CLEAN/structural).
"""

GREETING_TEMPLATE = "hello, {name}!"


def greet(name):
    """Return a greeting for name."""
    return GREETING_TEMPLATE.format(name=name)


def greet_all(names):
    """Return greetings for every name in order."""
    return [greet(name) for name in names]


def shout(text):
    """Uppercase a text and add emphasis."""
    return text.upper() + "!"


class Greeter:
    """A configurable greeter with its own template."""

    def __init__(self, template=GREETING_TEMPLATE):
        self.template = template

    def hello(self, name):
        """Greet one name with this greeter's template."""
        return self.template.format(name=name)
