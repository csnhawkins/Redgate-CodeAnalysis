# PI01 / PI02 - personal data (PII)

Gates any migration that writes to, drops, truncates, or renames a column or table tagged as holding
personal data. Full narrative writeup (architecture, wiring, rollout advice) lives in the root
[README.md](../../README.md); this page is the quick-reference for what the two rules actually do.

## What each rule catches

| Rule | Fires on |
|---|---|
| `PI01` | `INSERT`, `UPDATE`, `DELETE`, `MERGE` that writes a tagged column, or affects every column of a tagged table; `SELECT ... INTO` / `INSERT ... SELECT` that copy a tagged table into an untagged one |
| `PI02` | `DROP`, `TRUNCATE`, `ALTER TABLE` or `sp_rename` against a table holding tagged columns |

`PI01` cares about **writes, not reads** - a tagged column used only in a `WHERE` predicate, or a
tagged table joined/subqueried purely for lookups, is not reported. `PI02` matches at **table level
only** - any structural change to a table holding tagged columns needs a reviewer, regardless of which
specific column the change touches.

## What's tagged

The object list lives in [manifests/pii.json](../manifests/pii.json), not in the rule code. Edit that
file (or generate it from an upstream metadata/classification service) to change what's protected -
never edit [rules/pii.py](../rules/pii.py) to add or remove a tagged object.

Override the manifest path entirely with the `PII_MANIFEST_PATH` environment variable, e.g. for a
pipeline that generates a fresh manifest at the start of each run rather than reading the committed
file. See the root README.md section 4 for the trade-offs between a hand-maintained and a generated
manifest.

## Proving it fires

[tests/pii/V001__pii_violations.sql](../tests/pii/V001__pii_violations.sql) - every statement should be
reported. **20 `PI01`/`PI02` violations** (`PI01`: 13, `PI02`: 7) - verified against a real Flyway
Enterprise run. Other active rules (`RG01`, `RG09`, and a couple of Flyway's built-in checks) also fire
on the same file, since it isn't scoped to trip *only* the PII rules, so the file's total violation
count is higher than 20 - when asserting in CI, filter to `PI01`/`PI02` specifically rather than
counting every violation in the report.

[tests/pii/V002__clean.sql](../tests/pii/V002__clean.sql) - nothing should be reported by `PI01`/`PI02`.
**0 violations.** Every statement in this file is a case a naive text-matching rule gets wrong - read it
before touching `rules/pii.py`, since it's the regression suite for the read-vs-write and
alias-resolution logic in `rules/_shared.py`.

Run each file with `flyway check -code -check.scope="script" -check.scriptFilename="<path to the
file>"` - no separate scratch project or migrations folder needed, see
[tests/README.md](../tests/README.md) for the exact commands and how to pull the `PI01`/`PI02` count
out of the JSON report. Assert both counts in CI - see the root README.md section 6 for why the exit
code alone doesn't prove the gate is still enforcing anything.

## Known limitations

- Views and synonyms aren't resolved - tagging a base table doesn't catch DML against a view built on
  it.
- `sp_rename` detection is T-SQL specific; it doesn't port to `RENAME TABLE` on other dialects.
- A `MERGE` reading tagged data from its `USING` source (rather than `INSERT ... SELECT` or
  `SELECT ... INTO`) isn't reported as a copy.

Full limitations list: root README.md, section 11.
