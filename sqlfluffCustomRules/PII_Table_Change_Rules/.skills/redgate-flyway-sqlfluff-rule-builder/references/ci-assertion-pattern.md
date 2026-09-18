# The proof pair, and the CI assertion that makes it worth something

## Build two test migrations, not one

Check two files into the repo alongside the rule package:

1. **A "violations" file** where every single statement should be reported — one line per statement shape
   the rule claims to catch, including the awkward dialect-specific ones. If the rule covers `sp_rename`,
   test every literal shape it accepts (`table`, `schema.table`, `db.schema.table`, `table.column`,
   `schema.table.column`), both positional and named-argument forms — don't assume one shape covers all of
   them.
2. **A "clean" file** where nothing should be reported. This one is not a formality — every statement in
   it should be a case that a naive text-matching or overly literal rule gets wrong: a protected column
   read only in a predicate, a protected table joined or subqueried for reading only, a MERGE where the
   protected table is the `USING` source rather than the target, a copy between two objects that are both
   inside the protected classification, an object whose name merely appears inside a string literal or
   comment.

Run both through the rule and note the exact expected counts (e.g. "20 violations on file A, 0 on file B").
Those numbers are the contract.

## Why the exit code alone is not enough

Two independent failure modes produce a clean-looking result — `All Finished!`, exit code 0 — with zero
violations reported, and neither one is a real pass:

- **The file-size skip.** SQLFluff skips linting any file over its configured byte-size ceiling and reports
  zero violations for it, to avoid a parser lock on a very large file. Flyway's bundled SQLFluff raises the
  low upstream default, but the mechanism is still there — a large enough real migration silently gets
  skipped rather than linted.
- **The rule silently stopped loading or running.** A dropped import after a version bump, a rule code
  removed from the active `rules =` line in the SQLFluff config, a reserved-prefix collision, a manifest
  that's gone empty because a generator run broke. All of these produce the same "clean" result as an
  actually-clean migration.

A gate that returns success on a migration it never read, or a rule it never ran, is worse than no gate at
all — someone will point at the green build as evidence the control worked.

## The assertion

Run code review against the known-bad test file in its own pipeline step and assert the violation count is
at least what it should be — not just that the exit code is 0. Target the one test file directly with
`-check.scope="script"` and `-check.scriptFilename` rather than pointing `flyway.locations` at a whole test
folder — this needs no scratch project or real migrations folder, and doesn't touch the pipeline's real
`flyway.toml`:

```bash
flyway check -code -check.scope="script" -check.scriptFilename="code-review-rules/tests/pii/V001__pii_violations.sql" -reportFilename=gate-check.json

# Filter to the rule's own code(s) rather than counting every violation in the report - other
# active rules (the Redgate library, other custom rules) also fire on the same test file, and a
# change in their count shouldn't be able to mask this specific rule silently regressing.
COUNT=$(jq '[.individualResults[].results[].violations[] | select(.rule.code == "PI01" or .rule.code == "PI02")] | length' gate-check.json)
if [ "$COUNT" -lt 20 ]; then
  echo "Gate reported $COUNT PI01/PI02 violations on the test migration, expected 20."
  echo "The gate is not firing. Do not rely on code review until this is fixed."
  exit 1
fi
```

This single check catches, in one place, every one of: the object list was edited badly or a generator run
emptied it; a migration exceeded the file-size ceiling and got skipped; a Flyway/SQLFluff upgrade moved a
plugin import and the rule stopped loading; someone removed the rule's code from the active `rules` line.
Run it as its own pipeline step, and on a schedule as well as per build — a compliance control with no test
proving it still fires is not a control.

**Don't fix a file-size problem by setting the skip limit to zero.** Zero removes the ceiling entirely and
an unbounded file can take a very long time to parse. If a genuine migration exceeds the ceiling, split it —
large migrations are worth splitting anyway, since a partial failure mid-migration is harder to reason about
than a failure between two smaller ones.
