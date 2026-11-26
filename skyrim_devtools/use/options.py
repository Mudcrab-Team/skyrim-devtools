"""Session options: the robustness switches of an unattended playtest.

`NATIVE_OPTIONS` live in the plugin (Options.set); `PYTHON_OPTIONS` are handled by this client. The host config
may carry an `options` object with any of them, and the `game_options` tool changes them while a game runs.
"""

NATIVE_OPTIONS = ("alwaysActive", "autoDismissMenus", "cleanTransition", "cleanTransitionCell", "stallThresholdMs")
PYTHON_OPTIONS = ("strictArgs", "disableVanityCamera")


def split_options(options):
    """Check the keys of an options object and split it into (native, client) parts."""
    unknown = [key for key in options if key not in NATIVE_OPTIONS + PYTHON_OPTIONS]
    if unknown:
        raise ValueError(f"unknown option(s) {', '.join(map(repr, unknown))}; options are "
                          f"{', '.join(NATIVE_OPTIONS + PYTHON_OPTIONS)}")
    native = {key: value for key, value in options.items() if key in NATIVE_OPTIONS}
    client = {key: value for key, value in options.items() if key in PYTHON_OPTIONS}
    return native, client
