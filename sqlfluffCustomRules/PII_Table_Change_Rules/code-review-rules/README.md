# Custom Flyway code review rules

This folder is a single SQLFluff plugin package, meant to be dropped into `flyway.toml`'s
`check.sqlfluffCustomRulesPath` wholesale - see the root [README.md](../README.md) for the full
narrative writeup (architecture, wiring, gotchas, rollout advice). This file is the quick index:
what's in here, and how to add to it.

## What's registered right now

| Prefix | Concern | Rule module | Manifest | Doc |
|---|---|---|---|---|
| `PI01` / `PI02` | Personal data (PII) - DML and structural changes against tagged columns/tables | [rules/pii.py](rules/pii.py) | [manifests/pii.json](manifests/pii.json) | [docs/pii.md](docs/pii.md) |

## Folder layout

```
code-review-rules/
├── __init__.py          # SQLFluff plugin entry point - aggregates every domain's rules
├── sqlfluff.cfg          # which rule codes are active, and at what severity
├── rules/
│   ├── _shared.py        # generic manifest + parse-tree helpers - no domain knowledge, reused by all
│   └── pii.py             # Rule_PI01, Rule_PI02
├── manifests/
│   └── pii.json          # the object/column list PI01/PI02 protect - edit this, not rules/pii.py
├── docs/
│   └── pii.md             # what PI01/PI02 do, in plain English - read this before rules/pii.py
└── tests/
    ├── README.md          # how to run any domain's proof pair with -check.scope="script"
    └── pii/
        ├── V001__pii_violations.sql   # every statement should violate - the "is it firing" proof
        └── V002__clean.sql            # nothing should violate - the "is it too aggressive" proof
```

See [tests/README.md](tests/README.md) for the exact command to run any domain's proof pair without setting up a separate scratch Flyway project.

## Adding a new rule module

Say the next concern is "flag changes to financial tables." Here's the checklist, in order:

1. **Pick a free 2-letter prefix** and verify it against SQLFluff's actual class-naming regex before
   writing anything - a prefix that looks fine can still collide once padded with digits. See
   [docs/pii.md](docs/pii.md)'s note on this, or the `redgate-flyway-sqlfluff-rule-builder` skill for
   the exact verification snippet. Also check it isn't one of the reserved prefixes (`RG` is
   Redgate's; several 2-letter codes are reserved by stock SQLFluff).
2. **Write `rules/financial.py`**, following `rules/pii.py`'s shape: import shared helpers from
   `rules/_shared.py` (parse-tree walking, manifest loading/caching), define `Rule_FI01` /
   `Rule_FI02` classes, and call `_shared.load_manifest("financial.json", "FINANCIAL_MANIFEST_PATH")`
   for its manifest. Don't create a shared abstract base class between rule modules that itself
   inherits from `BaseRule` - SQLFluff validates every `BaseRule` subclass's name, and an intermediate
   helper class that isn't a real rule breaks the whole plugin at load time.
3. **Add `manifests/financial.json`** if the rule needs an object list (most compliance-classification
   rules do; a rule like "block migrations without a ticket reference" wouldn't).
4. **Register it in `__init__.py`**: import the new classes inside `get_rules()` and add them to the
   returned list.
5. **Add the new codes to `sqlfluff.cfg`'s `rules =` line** and a `[sqlfluff:rules:<name>]` section per
   rule - a rule that's registered but not listed there loads silently and never runs.
6. **Write the proof pair** in `tests/financial/` - one file where everything should violate, one
   where nothing should, covering the near-miss cases (reads vs writes, joins vs targets) the way
   `tests/pii/` does.
7. **Write `docs/financial.md`**, following `docs/pii.md`'s shape.
8. **Add a row to the table above.**

Nothing in `rules/_shared.py` should need to change for a new domain like this - if it does, that's a
sign the new rule needs something genuinely new (a different parse-tree question), not that the split
between "generic helpers" and "domain rules" was wrong.
