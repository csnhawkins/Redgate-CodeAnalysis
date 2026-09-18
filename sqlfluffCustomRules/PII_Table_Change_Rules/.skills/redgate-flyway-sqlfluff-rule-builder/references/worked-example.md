# Worked example

A complete, working, fully generic reference implementation exists — built for a customer PII-gating
engagement and then genericised so it carries no customer-specific content. It implements exactly the
pattern this skill describes: the `code-review-rules/` folder shape from `folder-structure.md`, two
rules (`PI01` for DML, `PI02` for structural changes), a JSON manifest, Flyway/SQLFluff wiring, and a
verified test pair (20 violations / 0 violations).

Ask whoever's driving the current session for the location of that example package (it typically sits in
whatever working folder the SE was using for the engagement it came from — check recent work rather than
assuming a fixed path, since it's a working example copied around per engagement rather than a permanent
fixture). Its root `README.md` is a full narrative writeup covering the same ground as this skill's
reference files, with real verified command output rather than illustrative snippets — useful to hand to
a customer directly, or to copy `code-review-rules/` wholesale as the starting point for a new rule set
rather than writing one from a blank file. Inside it:

- `code-review-rules/README.md` — the package index (prefix → concern → rule module → manifest → doc)
  and the "adding a new rule module" checklist, matching `folder-structure.md`.
- `code-review-rules/rules/_shared.py` — the generic manifest-loading and parse-tree helpers.
- `code-review-rules/rules/pii.py` — the two PII rule classes, built on `_shared.py`.
- `code-review-rules/manifests/pii.json`, `code-review-rules/docs/pii.md`,
  `code-review-rules/tests/pii/{V001__pii_violations.sql,V002__clean.sql}`, and
  `code-review-rules/tests/README.md` for the `-check.scope="script"` test command.

What to copy versus what to rewrite when adapting it to a new compliance concern:

- **Copy as-is**: everything in `rules/_shared.py` — the `Manifest` class, `_parse_manifest`,
  `load_manifest`, and the parse-tree helper functions (`ref_name`, `alias_map`, `target_table`,
  `all_tables`, `direct_tables`, `source_tables`, `column_names`, `written_columns`,
  `affects_whole_row`). None of this is PII-specific — it's generic SQL structure resolution, and a
  new concern's rule module should import from it rather than re-implementing any of it.
- **Rewrite as a new `rules/<concern>.py`**: the manifest's `objects` content (obviously), the
  violation message text, and the rule names/groups/prefix if the new concern isn't "personal data."
  The two-rule shape (one for DML against tagged columns/tables, one for structural change against
  tagged tables) is a reasonable default split for most classification-based compliance concerns, but
  a rule like "block migrations without a ticket reference" is a different shape entirely — it
  wouldn't need a manifest at all, just a check that a `-- JIRA-1234` style comment exists somewhere in
  the file.
- **Re-verify, don't assume unchanged**: any rule-code prefix. If the new concern gets a new 2-letter
  prefix rather than reusing `PI`, re-run the class-naming regex check from `plugin-architecture.md`
  before committing to it — don't assume a prefix that felt natural is actually free of the 4-character
  truncation collision.
