"""Custom Flyway code review rules for PII.

Exposes PI01 and PI02 to SQLFluff via the standard plugin hook.

Rule imports happen inside get_rules() on purpose: the package must finish
loading before SQLFluff's metaclass inspects the rule classes.

There is no get_configs_info() hook, because neither rule declares a SQLFluff
config parameter. The PII manifest path is resolved from this package
directory, or from the PII_MANIFEST_PATH environment variable.
"""

from sqlfluff.core.plugin import hookimpl


@hookimpl
def get_rules():
    from .rules import Rule_PI01, Rule_PI02
    return [Rule_PI01, Rule_PI02]
