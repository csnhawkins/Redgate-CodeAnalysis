"""Package marker for the rules/ subfolder.

Deliberately empty. Each compliance concern gets its own module here (pii.py today; a future
financial.py or naming.py would sit alongside it) - see code-review-rules/README.md for how to add
one. This file exists only so Python treats rules/ as an importable package; it should not accumulate
logic of its own. Shared logic belongs in _shared.py, not here.
"""
