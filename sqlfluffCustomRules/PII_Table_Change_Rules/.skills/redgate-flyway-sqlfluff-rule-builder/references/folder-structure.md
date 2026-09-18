# Folder structure — designed to scale past one rule

Don't start with a single flat `rules.py`. Even a first rule should go into this shape, because
retrofitting it later means moving code around while a rule is already live in a pipeline somewhere.
The shape exists to answer one question cleanly as more compliance concerns get added over time: "which
file do I touch to change X, and which files should I never need to touch to change X?"

```
code-review-rules/                    <- the whole thing: one self-contained SQLFluff plugin package
├── README.md                          <- index: prefix | concern | rule module | manifest | doc
├── __init__.py                        <- SQLFluff plugin entry point - aggregates every domain's rules
├── sqlfluff.cfg                       <- which rule codes are active, and at what severity
├── rules/
│   ├── _shared.py                     <- generic manifest + parse-tree helpers, NO domain knowledge
│   ├── pii.py                         <- Rule_PI01, Rule_PI02
│   └── financial.py                   <- Rule_FI01, Rule_FI02 (a second concern, added later)
├── manifests/
│   ├── pii.json                       <- the object/column list PI01/PI02 protect
│   └── financial.json
├── docs/
│   ├── pii.md                         <- what PI01/PI02 do, in plain English - not the code
│   └── financial.md
└── tests/
    ├── README.md                      <- how to run any domain's proof pair, one command
    ├── pii/
    │   ├── V001__pii_violations.sql   <- every statement should violate
    │   └── V002__clean.sql            <- nothing should violate
    └── financial/
        └── ...
```

## Why each split exists

- **`rules/_shared.py` vs `rules/<concern>.py`.** The parse-tree questions ("what table does this
  write to", "which columns does this set", "does this affect every column") are the same regardless
  of what's being protected. Write them once in `_shared.py`. Each concern's file then only contains
  the classification-specific parts: the manifest filename/env-var pair, the rule classes, and the
  violation message text. If a second concern's rule file ends up needing to duplicate logic from the
  first concern's file rather than pulling from `_shared.py`, that's the signal something belongs in
  `_shared.py` instead.
- **`manifests/` as its own folder, not inline in the rule file.** Covered in `manifest-pattern.md` —
  the short version is that this is the file a non-engineer reviewer edits and approves, so it needs to
  be found and read on its own, not buried inside Python.
- **`docs/<concern>.md` as its own file, separate from the code's docstrings.** A rule's docstring is
  for someone reading the Python. A `docs/*.md` page is for someone who was just told "there's a gate
  on personal data changes now" and wants to know what that means for their migration, without opening
  an IDE. Keep this page short: what triggers it, where the manifest is, expected test counts, known
  limitations. Point to the deeper narrative doc (if one exists) rather than duplicating it.
- **`tests/<concern>/` per concern, plus one `tests/README.md`.** Keeping each concern's proof pair in
  its own subfolder means you can lint just that concern's test files without a mixed count that's
  meaningless (see the note on this in `tests/README.md` below). The one shared `tests/README.md`
  documents the *mechanism* once — see the next section — rather than repeating it in every concern's
  folder.
- **One package `README.md` as an index, not a second copy of the deep-dive doc.** If a fuller
  narrative write-up exists elsewhere (an internal wiki page, a customer-facing doc), this file should
  point at it, not duplicate it. Its own job is just: what's registered, and the checklist for adding
  another rule module. See the checklist below.

## Running the proof pair without a scratch Flyway project

A common mistake is assuming the test migrations need their own throwaway Flyway project with its own
`flyway.toml`, or a real migrations folder to sit in. Neither is true — `check.scope="script"` reviews
one named script file directly, bypassing `flyway.locations` entirely:

```bash
flyway check -code -check.scope="script" -check.scriptFilename="code-review-rules/tests/pii/V001__pii_violations.sql"
```

This doesn't touch the real project's `flyway.toml`, so it's safe to run against a live project's
configuration without any risk of the test migrations being picked up as real ones to deploy. As long
as the dialect is set explicitly in `sqlfluff.cfg` (it should be, for exactly this reason — see
`requirements-and-gotchas.md`), no database connection is needed to run this.

**When asserting a count from this, filter by rule code rather than counting every violation in the
report.** Other active rules (the Redgate library, any other custom rule module in the package) will
also fire against the same test file, since it isn't scoped to trip only the one concern's rule — a
verified real run against a PII test file, for instance, reported 13 `PI01` + 7 `PI02` = 20 violations
for the PII rules specifically, alongside several more from unrelated active rules in the same report.
Counting the report's total would make the assertion fragile to unrelated rules' noise instead of
actually proving the rule under test still fires. Filter the JSON report's violations to the specific
rule code(s) before comparing against the expected count — see `ci-assertion-pattern.md` for the exact
`jq` shape.

Put the resulting command and the expected count for each concern into `tests/README.md`, in a table
that grows by one row per new concern, so nobody has to reverse-engineer the invocation from scratch six
months from now.

## Adding a new rule module — the checklist

1. Pick a free 2-letter prefix and verify it against the class-naming regex (see
   `plugin-architecture.md`) and against the reserved-prefix list (see
   `requirements-and-gotchas.md`) before writing anything.
2. Write `rules/<concern>.py`, importing shared helpers from `rules/_shared.py`. Do not create a
   shared abstract base class between concern files that itself inherits `BaseRule` — see
   `plugin-architecture.md` for why that breaks the whole plugin.
3. Write `manifests/<concern>.json` if the concern needs an object list (most classification-based
   concerns do; a rule like "block migrations without a ticket reference" wouldn't need one at all).
4. Register the new rule classes in `__init__.py`'s `get_rules()`.
5. Add the new codes to `sqlfluff.cfg`'s `rules =` line and a `[sqlfluff:rules:<name>]` section each —
   a registered-but-unlisted rule loads and silently never runs.
6. Write the proof pair in `tests/<concern>/`, add a row to `tests/README.md`'s expected-count table.
7. Write `docs/<concern>.md`.
8. Add a row to the package `README.md`'s index table.

Nothing in `rules/_shared.py` should need to change to do this. If it does, that's worth pausing on —
it usually means the new concern needs a genuinely new kind of parse-tree question, which is fine, but
worth being deliberate about rather than something that falls out accidentally.
