"""Custom Flyway code review rules - plugin entry point.

Flyway/SQLFluff discovers exactly one thing from this file: the get_rules() hook below, which must
return every rule class this package wants to register. This file's only job is to aggregate rules
from the rules/ subfolder - it must never contain rule logic, manifest handling, or parse-tree code
itself. See rules/_shared.py for the reusable logic every rule module builds on, and
code-review-rules/README.md for the index of every rule module currently registered here.

Two things about this file matter, and both will break the package if changed carelessly:

- Rule imports happen INSIDE get_rules(), not at module top level. SQLFluff's plugin loader must
  finish loading this whole package before its metaclass inspects the rule classes; importing them
  eagerly at module scope triggers a "Rule has been imported before all plugins have been fully
  loaded" warning and, on some SQLFluff versions, an outright load failure. Reference:
  https://docs.sqlfluff.com/en/stable/perma/plugin_dev.html
- There is no get_configs_info() hook here, because none of these rules declare a SQLFluff config
  parameter. Each rule module resolves its own manifest path itself (package-relative by default, or
  from an environment variable it defines) - see rules/pii.py for the pattern. Plugin config plumbing
  has varied between SQLFluff releases; keeping it out of the picture removes a class of
  version-dependent failure.

Adding a new rule module (e.g. a "financial" concern):

1. Write rules/financial.py, following rules/pii.py's shape - it should import shared helpers from
   `rules/_shared.py` and define its own Rule_XXnn classes. Verify any new 2-letter prefix against
   SQLFluff's class-naming regex BEFORE writing the rule classes - see docs/pii.md's note on this, or
   the redgate-flyway-sqlfluff-rule-builder skill, for how a prefix can silently collide.
2. Import its rule classes below and add them to the returned list.
3. Add the new rule codes to sqlfluff.cfg's `rules =` line, or they'll load but never actually run.
4. Add a docs/financial.md page and a row in this package's README.md index table.
"""

from sqlfluff.core.plugin import hookimpl


@hookimpl
def get_rules():
    from .rules.pii import Rule_PI01, Rule_PI02

    return [
        Rule_PI01,
        Rule_PI02,
        # Add future domains' rule classes here, e.g.:
        # Rule_FI01, Rule_FI02,   (from .rules.financial import Rule_FI01, Rule_FI02)
    ]
