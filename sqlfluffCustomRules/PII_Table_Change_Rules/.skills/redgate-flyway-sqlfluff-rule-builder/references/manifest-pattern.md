# The manifest pattern — separating rule logic from what it protects

## Why a manifest, not a hardcoded list

The rule's Python code should have zero knowledge of which specific objects it protects. That knowledge
lives in a JSON file the rule loads at runtime — the **manifest** — and it's the only file that should
change on a regular basis. Everything SQL-parsing-related stays in `rules/`, written once and left alone;
everything about *what's protected* sits in data, reviewed like any other change.

This split is what makes "tag one more column" a one-line pull request instead of a code change that needs
re-testing the rule logic.

## Shape

```json
{
  "generated": "2026-09-15T08:00:00Z",
  "source": "Metadata catalog, attribute IsPII=true",
  "objects": [
    { "schema": "dbo", "table": "Customer", "columns": ["SSN", "DateOfBirth", "TaxID"] },
    { "schema": "dbo", "table": "Employee", "columns": ["*"] },
    { "schema": "erp", "table": "Contact",  "columns": ["EmailAddress", "HomePhone"] }
  ]
}
```

- `"columns": ["*"]` tags the whole table — any DML against it is reported.
- An **empty** `columns` array must be rejected outright, not treated as a wildcard. Otherwise a generator
  bug (or a bad hand-edit) can silently upgrade one entry to whole-table enforcement.
- `generated` / `source` are documentation for whoever reads this file in a year. The rule itself ignores
  them.

## Two ways to maintain it — start with the simpler one

**Hand-maintained.** Edit the JSON, open a pull request, get the relevant stakeholder (compliance, a
platform owner, whoever owns the classification) to approve the diff. Fine for a pilot on the objects that
actually matter — it gets the gate running the same sprint, and the file is small enough for a
non-SQL-writing reviewer to read.

**Generated from an upstream metadata layer.** A pipeline step queries whatever system holds the
classification (a data catalog, a metadata service, an attribute on the schema model) and writes the file;
you commit the result. This is where things end up once hand-maintaining hundreds of columns across
multiple product lines stops scaling. Both paths produce the same file shape and the same rule code — start
hand-maintained, move to generated later without touching anything under `rules/`.

## Why commit the file rather than query live at lint time

Whichever way it's produced, the **committed** file is what the build should read, not a live query:

- The list a build was gated on is visible in the pull request diff — a reviewer can see that a column was
  tagged at the time a change was approved. That's the artifact an auditor asks for.
- A stale committed list shows up as a commit that hasn't moved — a visible problem. A metadata query that
  silently returns zero rows is invisible, and it fails open.
- The build agent running code review needs no database connection or credentials.

If a live query is genuinely needed, point an environment variable at a path the pipeline writes fresh at
the start of each run — the rule code doesn't need to change either way.

## Validation — fail loud, never half-load

A manifest that half-loads is more dangerous than one that fails outright, because it silently narrows what
is protected without telling anyone. Validate the shape strictly and turn every shape problem into a single,
clearly-worded lint violation rather than skipping the bad entry:

```python
def _parse_manifest(data):
    if not isinstance(data, dict):
        return Manifest(error="manifest root must be a JSON object")

    objects = data.get("objects")
    if not isinstance(objects, list):
        return Manifest(error="'objects' must be a JSON array")
    if not objects:
        return Manifest(error="'objects' is empty - nothing would be protected")

    for i, entry in enumerate(objects):
        # ... validate each entry's schema/table/columns shape explicitly,
        # returning a specific error naming the index and the field that's wrong.
        ...
```

The example worth internalizing: `"columns": "SSN"` instead of `"columns": ["SSN"]`. A permissive parser
that iterates a string's characters would silently protect the columns `S` and `N` instead of `SSN`. Reject
anything that isn't the exact expected type — don't coerce.

When the manifest is unusable, the rule itself should report exactly one violation naming the problem
(e.g. `"PII manifest unusable, gate not enforced: 'objects' is empty..."`), rather than reporting nothing.
Reporting nothing is indistinguishable from a clean pass; reporting the failure means the gate visibly tells
you it isn't enforced.

## Identifier matching

Compare names case-insensitively with brackets, quotes, and backticks stripped, so `[dbo].[Customer]`,
`dbo.Customer`, and `DBO.CUSTOMER` all match the same entry.

An unqualified reference like `UPDATE Customer SET ...` matches on table name alone. If two schemas both
have a `Customer` table and only one is tagged, this over-reports on the untagged one. That's the safe
direction for a compliance control — treat it as expected behaviour, not a bug, and if it gets noisy, fix it
by enforcing schema-qualified names elsewhere (a naming-convention rule) rather than loosening the match.

## Performance

Parse the manifest once per process and reduce it to sets/dicts for constant-time lookups — manifest size
then barely affects lint time. Measured on a 600-statement/36 KB migration against a manifest of 500 tables
and 5,000 tagged columns: two custom rules like this cost roughly +0.4s on top of a ~6.8s parse-only
baseline (~6%), versus +1.65s for a single stock layout rule that has to inspect every token rather than
just statement-level nodes. Re-run this kind of measurement against a real migration before quoting numbers
to a customer — the ratio is durable, the absolute time isn't.
