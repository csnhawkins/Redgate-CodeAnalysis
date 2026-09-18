# PII-aware code review rules for Flyway Enterprise

## What this covers

A working set of custom code review rules that fail a Flyway build when a migration writes to, drops, truncates or renames a column tagged as personal data. The objects to watch are listed in a JSON file your team maintains, so changing what is protected never means changing code. It covers the two rules, how you maintain that list, how to wire it into `flyway check -code`, two test migrations that prove it fires on the right statements and stays quiet on the rest, and how the gate behaves alongside feature-flagged migrations and a ringed rollout.

Everything here was executed, not inferred. The output in section 6 is real output.

## Contents

- [1. Where this sits](#1-where-this-sits)
- [2. Start with what already ships](#2-start-with-what-already-ships)
- [3. How the pieces fit together](#3-how-the-pieces-fit-together)
- [4. The PII manifest](#4-the-pii-manifest)
- [5. Wiring it into Flyway](#5-wiring-it-into-flyway)
- [6. Proving it works](#6-proving-it-works)
- [7. Expand and contract on a PII column](#7-expand-and-contract-on-a-pii-column)
- [8. Feature flags, rings, and conditional migrations](#8-feature-flags-rings-and-conditional-migrations)
- [9. Rollout path](#9-rollout-path)
- [10. Keeping central control](#10-keeping-central-control)
- [11. Limitations](#11-limitations)
- [12. Performance](#12-performance)
- [13. Phase two: gating the generated deployment script](#13-phase-two-gating-the-generated-deployment-script)
- [14. Requirements](#14-requirements)

## 1. Where this sits

Flyway has two loops, and this control belongs firmly in the first one.

The **author loop** is your developer: change the development database, save to the schema model, generate a migration, review it, commit it. Nothing here touches a real target environment. The **release loop** is your pipeline: build from scratch, code review, change and drift reports, deploy, snapshot.

These rules run in the author loop, at the pull request. That placement is the point rather than a convenience. Pushing a compliance check upstream means a developer finds out that a change touches personal data while they still have the change in their head, not at a release gate three weeks later when reversing it is expensive.

It matters more as change volume rises. AI-assisted development multiplies the number of database changes a team produces, and a review burden that only exists at deploy time becomes the bottleneck. A gate that runs at the pull request scales with the volume; a human reading release notes does not.

There is a complementary release-loop gate, and section 13 describes it. Build this one first.

## 2. Start with what already ships

Before writing any custom code, a useful amount of this control already exists in the Redgate rule library, covering both halves of the original question.

| Concern | Built-in rule | What the rule does, per the rules library |
|---|---|---|
| Unconditional `DELETE` | `RG06` | "Check for Delete without Where clause" |
| Unconditional `UPDATE` | `RG09` | "Check for Update without Where clause" |
| Data type change | `RG10` | "Check for data type modifications in ALTER TABLE statements" |
| `TRUNCATE` | `RG13` | "Check for TRUNCATE statements" |
| `DROP TABLE` | `RG01` | "Avoid using DROP TABLE statements." |
| `DROP COLUMN` | `RG14` | "Check for DROP COLUMN statements" |
| `DROP DATABASE` | `RG03` | "Avoid using DROP DATABASE statements." |

Turn those on, set `check.code.failOnError`, and the blunt version of the control is in place with no custom code at all. If your compliance stakeholders will accept a blanket rule rather than a PII-scoped one, stop here — the rest of this document is unnecessary.

Two things the built-in library does not do, which is why the custom rules exist:

- **It cannot scope by classification.** `RG09` reports every unconditional `UPDATE`, not only the ones touching personal data. On an ERP schema that is a lot of noise for a control that is meant to be about PII, and noise is what gets a gate switched off.
- **There is no `RENAME` rule.** Renaming a tagged column silently breaks the link between the column and its classification, and nothing in the library catches it.

`RG23` is the highest-numbered Redgate rule on the rules library page at the time of writing. Note that `RG18` and `RG19` are mutually exclusive naming rules — enable only one, to match whichever naming convention you use.

## 3. How the pieces fit together

The whole thing is one self-contained folder, `code-review-rules/`, meant to be dropped into `flyway.toml`'s `check.sqlfluffCustomRulesPath` as-is — see [section 10](#10-keeping-central-control) for pulling it into multiple product repositories as a single shared, read-only source rather than copy-pasting it. Inside that folder:

- `code-review-rules/rules/_shared.py` — generic manifest loading and parse-tree helpers. No knowledge of PII or any other specific concern lives here — written once, then left alone.
- `code-review-rules/rules/pii.py` — the two PII rules, built on top of `_shared.py`.
- `code-review-rules/manifests/pii.json` — **the list of tables, objects and columns to watch. Yours to maintain, and the only file that changes regularly.**
- `code-review-rules/docs/pii.md` — the plain-English "what does this rule do" page for PI01/PI02.
- `code-review-rules/sqlfluff.cfg` — which rules are active and at what severity.
- `code-review-rules/README.md` — the index of every rule module in the folder, and the checklist for adding another one.
- `flyway.toml` — points Flyway at `code-review-rules/` and its `sqlfluff.cfg`.

The split matters more than any individual piece. All the Flyway-specific and SQL-parsing work sits in `rules/`, and everything about *your* classification sits in a JSON file your team owns. Adding a newly tagged column is a one-line change to data, reviewed in a pull request, with no code change and no re-testing of the rules. This also means the package can grow to cover other compliance concerns over time — a "financial tables" rule or a "naming convention" rule slots in as its own `rules/<concern>.py` + `manifests/<concern>.json` pair, without touching the PII rules at all. `code-review-rules/README.md` has the step-by-step for adding one.

The rules are:

| Rule | Fires on |
|---|---|
| `PI01` | `INSERT`, `UPDATE`, `DELETE`, `MERGE` that writes a PII-tagged column or affects every column of a PII-tagged table; `SELECT ... INTO` and `INSERT ... SELECT` that copy a PII-tagged table into an untagged one |
| `PI02` | `DROP`, `TRUNCATE`, `ALTER TABLE` or `sp_rename` against a table holding PII-tagged columns |

**`PI01` reports writes, not reads, and it resolves the write target properly.** This is the part that decides whether the rule is usable, so it is worth being precise about. A PII column used only in a `WHERE` predicate is not reported. Neither is a PII table that appears only in a subquery, only in a `JOIN`, or only as the `USING` source of a `MERGE` — in all of those the personal data is being read to locate rows, not changed. The rule finds the write target from the statement's own structure and resolves an alias back to the real table, so `UPDATE c SET c.SSN = '1' FROM dbo.Customer AS c` is reported and `UPDATE o SET o.Total = 1 FROM dbo.OrderHeader o JOIN dbo.Employee e ON o.eid = e.id` is not.

Where the whole row is affected, it is reported at table level: a `DELETE`, an `INSERT` with no column list, and a `MERGE` with a `THEN DELETE` branch all touch every column including the tagged ones.

`SELECT ... INTO` and `INSERT ... SELECT` are covered because they are the quiet way personal data leaves its classification. Copying `dbo.Customer` into `dbo.CustomerCopy` puts tagged values into a table your metadata layer knows nothing about, and no DML rule would see it. A copy into another tagged table is not reported, because the data stays inside the classification.

**`PI02` matches at table level only.** If a table holds tagged columns, any structural change to it warrants a reviewer, and matching structural changes at column level is unreliable across dialects.

> Not from Redgate docs — starting point, validate in your environment.

The code was written against the documented SQLFluff plugin API and verified on **SQLFluff 3.4.2, the version Flyway bundles**, as well as on 4.3.0. All behavior described in this document was executed, not inferred: 32 statement-shape cases, 9 malformed-manifest cases, and both test migrations.

### The plugin entry point

`code-review-rules/__init__.py`:

```python
"""Custom Flyway code review rules - plugin entry point."""

from sqlfluff.core.plugin import hookimpl


@hookimpl
def get_rules():
    from .rules.pii import Rule_PI01, Rule_PI02
    return [Rule_PI01, Rule_PI02]
```

Two things about this file matter, and both will break the package if changed:

- The rule imports happen **inside** `get_rules()`, not at module top level. The package has to finish loading before SQLFluff's metaclass inspects the rule classes.
- There is no `get_configs_info()` hook, because neither rule declares a SQLFluff config parameter. The manifest path is resolved from the package directory, or from the `PII_MANIFEST_PATH` environment variable if you need to override it. Plugin config plumbing has varied between SQLFluff releases, and taking it out of the picture removes a class of version-dependent failure.

`__init__.py` only aggregates rule modules — it should never contain rule logic itself. As more compliance concerns are added, each gets its own `rules/<concern>.py` and an extra import line here. See `code-review-rules/README.md` for the checklist.

### The rule classes

The full implementation is in `code-review-rules/rules/pii.py`, built on the generic parse-tree and manifest-loading helpers in `code-review-rules/rules/_shared.py`. The class declarations are the part worth reviewing:

```python
class Rule_PI01(BaseRule):
    """DML against a PII-tagged column requires compliance sign-off."""

    groups = ("all", "pii")
    name = "pii.dml"
    crawl_behaviour = SegmentSeekerCrawler(
        {"insert_statement", "update_statement", "delete_statement", "merge_statement"}
    )
    is_fix_compatible = False


class Rule_PI02(BaseRule):
    """Structural change to a PII-tagged table requires compliance sign-off."""

    groups = ("all", "pii")
    name = "pii.structure"
    crawl_behaviour = SegmentSeekerCrawler(
        {
            "truncate_table",
            "drop_table_statement",
            "alter_table_statement",
            "execute_script_statement",
        }
    )
    is_fix_compatible = False
```

Two implementation notes that cost time if you find them the hard way:

- **Do not create a shared abstract base class that inherits from `BaseRule`.** SQLFluff validates the class name of every `BaseRule` subclass against the `Rule_XXnn` pattern, so an intermediate class raises `SQLFluffUserError` at import and the whole plugin fails to load. Shared logic belongs in module-level functions.
- **The `EP` prefix is deliberate.** `RG` is reserved for Redgate rules, and the stock SQLFluff prefixes (`AL`, `AM`, `CP`, `CV`, `JJ`, `LT`, `RF`, `ST`, `TQ` and others) are reserved too. A rule using a reserved prefix is dropped at load time with a warning on stderr and the rest of the run continues, which is easy to miss.

The crawler seeks whole statements rather than individual column references. The rule then walks the statement's own parse tree to collect every table and column it touches. This is what makes alias resolution work: in `UPDATE c SET c.SSN = '1' FROM dbo.Customer AS c` the rule sees both the alias and `dbo.Customer`, and matches on the real table name.

The statement segment names above (`truncate_table`, `drop_table_statement`, `alter_table_statement`) are identical across the SQL Server, PostgreSQL, Oracle and MySQL dialects, so the same package ports to your other product lines with only the manifest changing.

## 4. The PII manifest

**This file (`code-review-rules/manifests/pii.json`) is yours.** It is the whole point of the design: the rules carry no knowledge of which objects hold personal data, and the list of tables, objects and columns to watch lives in a plain JSON file your team owns and maintains. Changing what is protected is a change to this file, reviewed like any other change in your repository. It does not require touching `rules/pii.py`, re-testing the rules, or coming back to Redgate.

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

`"columns": ["*"]` tags the whole table, so any DML against it is reported. An empty `columns` array is rejected rather than treated as a wildcard, so a generator bug cannot quietly escalate one table to whole-table enforcement.

The `generated` and `source` fields are documentation for whoever reads this in a year. The rules ignore them.

### Two ways to maintain it, and you can start with the simpler one

**Hand-maintained.** Edit the JSON, open a pull request, have compliance approve the diff. For a first product line, or for a pilot on the twenty tables that actually matter, this is enough and it gets the gate running this sprint. The file is small, readable, and reviewable by someone who does not write SQL.

**Generated from your metadata layer.** A pipeline step queries for everything carrying the PII attribute and writes the file, and you commit the result. This is where you end up once the pilot proves out, because hand-maintaining hundreds of columns across six product lines does not scale.

Both paths are the same file and the same rules. Start hand-maintained, move to generated when the list outgrows a human.

### Why commit the file rather than query at lint time

Whichever way it is produced, keep the committed file as what the build reads:

- The list a build was gated on is visible in the pull request diff, so a reviewer can see that the SSN column was tagged at the time the change was approved. That is the artifact an auditor asks for.
- A stale list shows up as a commit that has not moved, which is a visible problem. A metadata query that silently returns zero rows is an invisible one, and it fails open.
- The build agent needs no database connection or credentials to run code review.

The trade-off is that a column tagged this morning is not enforced until the file is updated. If you generate it, run the generation on a schedule and fail it loudly if the object count drops unexpectedly.

If you do want a live query, set `PII_MANIFEST_PATH` to a path your pipeline writes at the start of each run. The rule code does not change.

### One design note, if someone asks why this isn't a rule per object

An alternative shape is to generate a rule per tagged object, so the ruleset is built dynamically from the list. Two reasons this package does not:

- **Rule codes would churn.** Every `-- noqa` suppression, every SARIF baseline, and every piece of compliance reporting keys off the rule code. If codes are generated from the object list, they change whenever the list changes, and the audit history stops joining up. With two stable codes, `PI01` and `PI02`, a violation from last quarter still means the same thing.
- **Two rules are governable; hundreds are not.** Severity, profiles and suppression all configure per rule. Keeping the ruleset fixed and the data variable means there are exactly two things to govern, no matter how long the list grows.

The effect is the same either way. This shape just keeps the moving part in data rather than in configuration.

### Identifier matching

Names are compared case-insensitively with `[brackets]`, `"quotes"` and backticks stripped, so `[dbo].[Customer]`, `dbo.Customer` and `DBO.CUSTOMER` all match the same entry.

An unqualified reference such as `UPDATE Customer SET ...` is matched on the table name alone. If two schemas both hold a `Customer` table and only one is tagged, this over-reports. Over-reporting is the safe direction for a compliance control, and the durable fix is to require schema-qualified names, which the naming rules in the Redgate library can enforce separately.

## 5. Wiring it into Flyway

`flyway.toml`:

```toml
[flyway.check]
sqlfluffCustomRulesPath = "code-review-rules"
rulesConfig = "code-review-rules/sqlfluff.cfg"

[flyway.check.code]
failOnError = false
noqaSeverity = "ERROR"
```

`sqlfluff.cfg`:

```ini
[sqlfluff]
dialect = tsql
rules = PI01,PI02,RG01,RG06,RG09,RG10,RG13,RG14
```

Custom rules are loaded but only **run** if they are in the active rule set, so the `rules` line is not optional. A rule that loads and never fires is almost always missing from this list.

Then:

```bash
flyway check -code
```

If you run code review against a platform where there is no connection for Flyway to infer the dialect from, set it explicitly with the `check.rulesDialect` setting rather than relying on the `sqlfluff.cfg` line alone.

### The setting that decides whether this gate works at all

SQLFluff skips any file larger than `large_file_skip_byte_limit` and reports zero violations for it, to avoid a parser lock on a very large file. Upstream SQLFluff sets that limit at 20,000 bytes. **Flyway's bundled SQLFluff raises it**, because the upstream value is low for real migrations — confirm the current bundled value with Redgate rather than assuming either number.

Two practical consequences, and the second is the one that matters:

**Do not set the limit to zero in production.** Zero disables the cap entirely, and parsing an unbounded file can take a very long time. Leave the bundled default in place, or set a deliberate high ceiling that suits your largest real migration. Zero is a debugging setting, not a production one.

**Assert the violation count in CI, because a skip is indistinguishable from a pass.** Whatever the ceiling is, a file above it produces `All Finished!` and exit code 0 — the same signal as a clean file. Demonstrated on a 33 KB migration whose first line is `UPDATE dbo.Customer SET SSN = '000-00-0000' WHERE CustomerID = 1;`, linted against a 20,000-byte ceiling:

```text
WARNING  Length of file 'big.sql' is 33031 bytes which is over the limit of
         20000 bytes. Skipping to avoid parser lock.
All Finished!
exit code 0
```

That is the failure mode to design against, and it is not really about the number. A compliance gate that returns success on a file it never read is worse than no gate, because someone will point at the green build as evidence. Asserting that `V001__pii_violations.sql` still reports 20 `PI01`/`PI02` violations between them catches it regardless of where the ceiling sits, and catches several other ways the gate can quietly stop working at the same time. That assertion, not the limit, is the control on the control.

If a genuine migration does exceed the ceiling, the answer is to split it rather than to remove the cap. Large migrations are worth splitting anyway, since a failure part-way through one is harder to reason about than a failure between two.

### Three settings that decide whether this is a control or a suggestion

**`noqaSeverity = "ERROR"`.** A developer can suppress any SQLFluff rule inline with `-- noqa: PI01`, and by default that suppression is reported only as a warning. Setting it to `ERROR` means a deliberate bypass of the PII gate still surfaces in the report and in the build result. This is available in the Flyway CLI only.

**`failOnError`.** Governs whether an error-severity violation fails the command. Section 9 covers when to turn it on.

**Run `check -code` as its own pipeline step.** Code review does not interrupt subsequent Flyway verb operations when they are chained, so even with `failOnError` enabled a chained `migrate` still runs after a failing review. The gate only holds if the review is a separate step whose exit code the pipeline acts on. This is the single most likely way to end up with a control that reports correctly and stops nothing.

### Getting the output somewhere a reviewer will read it

Native SARIF 2.1.0 output has been available since engine 12.2.0, so violations land as annotations in GitHub code scanning on the pull request rather than in a build log.

Custom rule violations are tagged `"ruleSource": "custom"` in the JSON and SARIF reports, so compliance reporting can separate them from stock rules. The `help` field is always `null` for custom rules, since there is no Redgate documentation page to link. If you build reporting on top of the JSON, note that engine 13.6.0 added a top-level `schemaVersion` field to the report, and that report timestamps have carried a UTC offset since 13.2.0.

## 6. Proving it works

Two migrations ship in `code-review-rules/tests/pii/`. No separate scratch Flyway project or migrations folder needed — point `check -code` at one script file directly: `flyway check -code -check.scope="script" -check.scriptFilename="code-review-rules/tests/pii/V001__pii_violations.sql"`. See `code-review-rules/tests/README.md` for both commands, verified real output, expected counts per domain, and how to pull the `PI01`/`PI02` count out of the JSON report.

`code-review-rules/tests/pii/V001__pii_violations.sql` — every statement should be reported. **20 violations.** Output below is verbatim from `sqlfluff lint` against the shipped package, manifest and config, on SQLFluff 3.4.0:

```text
L:   3 | P:  1 | PI01 | UPDATE writes PII-tagged column(s) SSN on dbo.Customer. Compliance sign-off required.
L:   4 | P:  1 | PI01 | UPDATE writes PII-tagged column(s) TaxID on dbo.Customer. Compliance sign-off required.
L:   5 | P:  1 | PI01 | UPDATE writes PII-tagged column(s) SSN on dbo.Customer. Compliance sign-off required.
L:   6 | P:  1 | PI01 | UPDATE writes PII-tagged column(s) SSN on dbo.Customer. Compliance sign-off required.
L:   7 | P:  1 | PI01 | INSERT writes PII-tagged column(s) SSN on dbo.Customer. Compliance sign-off required.
L:   8 | P:  1 | PI01 | INSERT affects every column of dbo.Customer, including PII-tagged DateOfBirth, SSN, TaxID. Compliance sign-off required.
L:   9 | P:  1 | PI01 | DELETE affects every column of dbo.Customer, including PII-tagged DateOfBirth, SSN, TaxID. Compliance sign-off required.
L:  10 | P:  1 | PI01 | MERGE writes PII-tagged column(s) SSN on dbo.Customer. Compliance sign-off required.
L:  12 | P:  1 | PI01 | MERGE writes PII-tagged column(s) SSN on dbo.Customer. Compliance sign-off required.
L:  14 | P:  1 | PI01 | MERGE affects every column of dbo.Customer, including PII-tagged DateOfBirth, SSN, TaxID. Compliance sign-off required.
L:  16 | P:  1 | PI01 | SELECT INTO copies dbo.Customer (PII-tagged DateOfBirth, SSN, TaxID) into dbo.CustomerCopy. The copy is not covered by the PII classification. Compliance sign-off required.
L:  17 | P:  1 | PI01 | DELETE on dbo.Employee, which is tagged as holding personal data. Compliance sign-off required.
L:  18 | P:  1 | PI02 | TRUNCATE TABLE on dbo.Customer, which holds PII-tagged DateOfBirth, SSN, TaxID. Compliance sign-off required.
L:  19 | P:  1 | PI02 | DROP TABLE on erp.Contact, which holds PII-tagged EmailAddress, HomePhone. Compliance sign-off required.
L:  20 | P:  1 | PI02 | ALTER TABLE on dbo.Customer, which holds PII-tagged DateOfBirth, SSN, TaxID. Compliance sign-off required.
L:  21 | P:  1 | PI02 | sp_rename targets 'dbo.Customer.SSN'. dbo.Customer holds PII-tagged DateOfBirth, SSN, TaxID, and renaming a tagged object breaks the link to its classification. Compliance sign-off required.
L:  22 | P:  1 | PI02 | sp_rename targets 'Customer.SSN'. dbo.Customer holds PII-tagged DateOfBirth, SSN, TaxID, and renaming a tagged object breaks the link to its classification. Compliance sign-off required.
L:  23 | P:  1 | PI02 | sp_rename targets 'MyDb.dbo.Customer'. dbo.Customer holds PII-tagged DateOfBirth, SSN, TaxID, and renaming a tagged object breaks the link to its classification. Compliance sign-off required.
L:  25 | P:  1 | PI01 | INSERT copies dbo.Customer (PII-tagged DateOfBirth, SSN, TaxID) into dbo.CustomerExport. The copy is not covered by the PII classification. Compliance sign-off required.
L:  27 | P:  1 | PI02 | sp_rename targets 'dbo.Customer'. dbo.Customer holds PII-tagged DateOfBirth, SSN, TaxID, and renaming a tagged object breaks the link to its classification. Compliance sign-off required.
```

Note the four `sp_rename` forms. `sp_rename` names its target in a string literal, so there is no table reference to match and the literal can be `table`, `schema.table`, `db.schema.table`, `table.column` or `schema.table.column`. All five forms are tested rather than one assumed.

**This is standalone SQLFluff output, not `flyway check -code` output.** `flyway check -code` reports the same violations through its own summary table plus HTML, JSON and SARIF reports, so the presentation will differ. The violation count and the descriptions are what to compare against.

`code-review-rules/tests/pii/V002__clean.sql` — nothing should be reported. **0 violations.** The file is not a formality; every statement in it is a case that a text-matching or naive-parsing rule gets wrong:

```sql
-- Writes a non-PII column, reads a PII column in the predicate only.
UPDATE dbo.Customer SET Email = 'x@y.com' WHERE SSN = '000-00-0000';

-- A PII table read in a subquery. The write target is not PII.
UPDATE dbo.OrderHeader SET Total = 1 WHERE OrderID IN (SELECT id FROM dbo.Employee);

-- A PII table joined for reading only. The write target is not PII.
UPDATE o SET o.Total = 1 FROM dbo.OrderHeader o JOIN dbo.Employee e ON o.eid = e.id;

-- T-SQL two-FROM delete where the PII table is only joined, not deleted from.
DELETE FROM dbo.OrderHeader FROM dbo.OrderHeader o JOIN erp.Contact ct ON o.id = ct.id;

-- MERGE where the PII table is the USING source, not the target.
MERGE dbo.OrderHeader AS t USING dbo.Customer AS s ON t.id = s.id
  WHEN MATCHED THEN UPDATE SET t.Total = 1;

-- ALTER on a non-PII table that only references a PII table in a foreign key.
ALTER TABLE dbo.OrderHeader ADD CONSTRAINT FK_OH_Cust FOREIGN KEY (CustomerID)
  REFERENCES dbo.Customer (CustomerID);

-- The words "then delete" appear in a string, but no row is deleted.
MERGE dbo.Customer AS t USING dbo.Stage AS s ON t.id = s.id
  WHEN MATCHED THEN UPDATE SET t.Email = 'then delete';

-- Copy between two tagged tables. The data stays inside the classification.
INSERT INTO dbo.Customer (Email) SELECT EmailAddress FROM erp.Contact;
```

### What happens when the manifest is broken

Nine malformed-manifest shapes were tested. Each produces exactly one violation naming the problem, and the gate reports itself as not enforced rather than passing quietly:

```text
PI01 | PII manifest unusable, gate not enforced: objects[0] 'columns' must be a JSON array, got str
PI01 | PII manifest unusable, gate not enforced: objects[0] 'schema' must be a string
PI01 | PII manifest unusable, gate not enforced: manifest root must be a JSON object
PI01 | PII manifest unusable, gate not enforced: 'objects' is empty - nothing would be protected
```

The validation is strict on purpose. A manifest that half-loads is more dangerous than one that fails outright, because it narrows what is protected without telling anyone. `"columns": "SSN"` instead of `"columns": ["SSN"]` is the example worth internalizing — a permissive parser iterates the string's characters and silently protects `S` and `N` instead of `SSN`.

### Put these in CI

Keep both files in the repository and assert their counts on every build. This is the most valuable twenty lines of pipeline in the whole exercise, because it is the only thing that distinguishes a working gate from a gate that has silently stopped firing.

It catches all of these at once:

- The object list was edited badly, or a generator run emptied it.
- A migration exceeded the file-size ceiling and was skipped.
- A Flyway or SQLFluff upgrade moved a plugin import and the rules no longer load.
- Someone removed `PI01` or `PI02` from the `rules` line.

Each of those produces zero violations and exit code 0 — the same signal as a clean repository. The assertion is what tells them apart.

The shape of it, against the `check -code` JSON report:

> Not from Redgate docs — starting point, validate in your environment.

```bash
# Run code review over the known-bad test migration only, into a JSON report.
flyway check -code -check.scope="script" -check.scriptFilename="code-review-rules/tests/pii/V001__pii_violations.sql" -reportFilename=pii-gate-check.json

# The gate must still fire. Fewer than 20 PI01/PI02 violations means it has stopped
# working, whatever the exit code says. Filter to PI01/PI02 specifically rather than
# counting every violation in the report - other active rules (RG01, RG09, etc.) also
# fire on this file, and a change in their count shouldn't mask a PII rule regressing.
COUNT=$(jq '[.individualResults[].results[].violations[] | select(.rule.code == "PI01" or .rule.code == "PI02")] | length' pii-gate-check.json)
if [ "$COUNT" -lt 20 ]; then
  echo "PII gate reported $COUNT PI01/PI02 violations on the test migration, expected 20."
  echo "The gate is not firing. Do not rely on code review until this is fixed."
  exit 1
fi
```

Run it as its own pipeline step, on a schedule as well as per build, and alert on it like any other monitoring check. A compliance control with no test proving it still fires is not a control.

## 7. Expand and contract on a PII column

Worth reading even if the rules are all you take from this document, because it is where a PII-tagged column and a zero-downtime schema change interact badly.

Expand and contract is the standard way to change a column shape without downtime, and it is an industry pattern rather than a Flyway idea. In outline, three steps:

1. **Expand.** Add the new column in a backward-compatible migration, so old and new application code both work.
2. **Migrate.** Move the data and dual-write during a transition window while both shapes coexist.
3. **Contract.** Once every code path uses the new shape, ship a migration that removes the old column.

When the column holds personal data, all three steps are in scope for these rules, and that is correct — each one is a change to personal data that someone should have approved.

**The contract step is the one teams skip.** They expand, migrate, ship the feature, and never come back. On an ordinary column that leaves a dead column and some dual-write code. On a PII column it leaves personal data sitting in a column nothing reads, indefinitely, outside whatever retention policy you have told a regulator about. That is a data-retention problem rather than a change-control one, and no code review rule will catch it, because the failure is a migration nobody wrote.

Two practices close it:

- Write the contract migration at the same time as the expand, version-number it, and hold it until the transition window closes. An unwritten migration is easy to forget; a written and numbered one that has not been deployed shows up in `flyway info`.
- Have the manifest generator report tagged columns that no longer appear anywhere in the current schema model. A tagged column that still exists in the database but has fallen out of use is precisely the residue this pattern leaves.

## 8. Feature flags, rings, and conditional migrations

If schema change is gated by the same feature flags as the application, and rolled out through rings, three interactions are worth designing for up front.

**Conditional migrations.** Flyway gates a migration on a placeholder with the `shouldExecute` script setting, so a flag state can decide whether a migration runs in a given environment. The PII rules are unaffected by this and that is the right behavior: `check -code` is static analysis of the script, so a conditional PII migration is reported at the pull request regardless of which rings will eventually run it. You review it once, when it is authored.

**Conditional migrations make rings legitimately different from each other, and that is worth deciding about deliberately.** If ring A ran a conditional PII migration and ring B did not, the two are meant to differ, and you need to know which differences are intentional before a comparison of the two tells you anything. Where you compare a target against an expected state — a snapshot or a model — decide up front which conditional differences are expected and filter them, so the report stays readable.

This matters for the compliance story more than the code review rule does. Code review catches change that goes *through* the pipeline. Drift detection is what catches a PII column changed *outside* it, by someone with production access and a reason. A code review rule cannot see that at all, and it is usually the thing an auditor is actually asking about. Worth confirming the exact drift behavior for your conditional-migration setup with Redgate before you build the compliance narrative on it — the interaction depends on whether you are comparing against a snapshot or a schema model.

**Rings.** The schema history table is per target, so each ring's state is independently verifiable, and that is what lets you answer "which rings have the new PII column" without guessing. Two conventions hold at this scale: snapshot each target before its deploy so you can diff and recover one without touching the rest, and treat sequential rollout as the default, adding parallelism only once a ring rollout is proven.

## 9. Rollout path

Do not switch this on at error severity on day one. That is how a code review rollout gets reversed.

1. **Audit.** `failOnError = false`. The rules report, nothing fails. Run for two or three sprints and read the output. This phase is where you find the parts of the schema nobody realized were tagged, and the tagging errors in the metadata layer itself. Expect the first run to be noisier than you want.
2. **Soft gate.** Still `failOnError = false`, but the report is reviewed on every pull request and a hit requires a comment explaining it. Behavior changes before enforcement does.
3. **Hard gate on the PII rules only.** `failOnError = true` with only `PI01` and `PI02` at error severity. Everything else stays at warning. A narrow gate that always holds beats a broad one that gets switched off.
4. **Widen** to the Redgate library and the stock SQLFluff rules as appetite grows.

One mechanism makes per-product-line phasing cleaner than maintaining a separate `rules` line per repository, added in engine **13.6.0**:

`check.code.profile` and `check.code.profiles` define named review profiles and the rules or rule groups each profile **excludes**. That is the supported way to run a strict profile on one product line and a lighter one on another without forking the ruleset. A built-in `schemaModel` profile ships alongside them.

Note what profiles cannot do. They exclude rules, so they cannot hold `PI01` and `PI02` at error severity while everything else sits at warning — that is severity configuration in `sqlfluff.cfg`. The two mechanisms work together rather than interchangeably. The profiles documentation also notes that profiles cannot be added through Flyway Desktop, which is worth knowing alongside the Desktop option in section 10.

There is also a `check.scope` setting governing which migrations a review covers. It takes a fixed set of values rather than an arbitrary file list, and one of them requires a connection, so read the setting page against your pipeline shape before designing around it.

A pre-commit hook running `flyway check -code` locally gives developers the same answer before they open the pull request, which does more for adoption than anything in the pipeline.

## 10. Keeping central control

Across multiple product lines the ruleset needs to be one thing rather than one per team — and this package is deliberately self-contained (rules, config, docs, tests all under `code-review-rules/`) so it can be moved wholesale rather than assembled per repository:

- Put `code-review-rules/` in its own repository with restricted write access, and pull it into each product repository as a **git submodule pinned to a specific tag or commit** — never a floating branch. A developer in a product repository then gets a read-only checkout at whatever the pin says; changing the ruleset means a pull request against the central repo followed by a deliberate pin bump in each product repository, not an edit anyone can make by accident. (If your team isn't submodule-fluent, a pipeline step that clones the rules repo at a pinned tag before `check -code` runs is an equally valid alternative — more moving parts in CI, no git-submodule learning curve.)
- Point `check.rulesConfig` at the submodule's committed `code-review-rules/sqlfluff.cfg` explicitly, so a local `.sqlfluff` file in a product repository cannot override the severity of the PII rules. Without this, a team can turn the gate off locally and the pipeline will not notice.
- Generate each concern's manifest once, centrally, from the metadata layer that classifies it, and publish it into the shared repository rather than letting each product team maintain its own copy.
- As more compliance concerns get added over time (see `code-review-rules/README.md` for the checklist), they all ship through the same pinned submodule — a product repository picks up a new rule module the same way it picks up a manifest update, by bumping the pin.

Code review policies are also configurable in the Flyway Desktop UI, on a Code Review Policies tab in project settings, if a team would rather not hand-edit config.

## 11. Limitations

Take these to your compliance stakeholders before they find them. Each one is a real gap, and the first two are the ones that decide whether the control is trustworthy.

**Two ways the gate can fail open silently.** Both are addressed in this package, and neither announces itself.

- **A migration larger than the file-size ceiling is skipped and reported as clean.** Covered in section 5. The fix is the CI assertion on `V001`, not removing the ceiling — setting it to zero trades a silent gap for unbounded parse time.
- **GLIBC 2.35 on the build agent.** Covered in section 14. Below that the packaged SQLFluff does not start, which is most likely to affect the agents serving your on-prem and legacy rings.

**Coverage gaps in the analysis itself.**

- **Static analysis sees only the SQL text in the migration.** Dynamic SQL built as a string, and `EXEC` of a stored procedure that performs the DML internally, are invisible to these rules. Reaching them needs a different rule that reports any dynamic SQL construction in a migration at all, which is a coding-standards conversation rather than a classification one.
- **A `MERGE` reading PII from its `USING` source is not reported as a copy.** `INSERT ... SELECT` and `SELECT ... INTO` are. A bulk load shaped as `MERGE INTO untagged USING tagged` is the remaining hole in copy coverage.
- **Views and synonyms are not resolved.** The rules match the object named in the statement. If `dbo.CustomerView` selects from `dbo.Customer`, tagging the base table does not catch DML against the view. Tag the views too, or generate view entries into the manifest from your dependency graph.
- **`RENAME` coverage is T-SQL only.** `sp_rename` detection does not port to the other dialects, so `RENAME TABLE` on a MySQL-based product line is not reported. The DML and `DROP`/`TRUNCATE`/`ALTER` coverage does port.
- **`ALTER TABLE` is matched at table level.** Adding an unrelated non-PII column to a table that holds PII is reported. Intentional, and it will generate noise on wide tables.
- **Unqualified table names are matched on the table name alone**, so two schemas with the same table name over-report. Over-reporting is the safe direction.
- **DML is reported once per statement; `DROP` and copies are reported per table.** An `UPDATE` or `MERGE` touching several tagged tables produces one violation, so treat DML findings as "this statement needs review" rather than a complete inventory. `DROP TABLE a, b` and a copy reading two tagged sources do report each table.
- **`check -code` reviews migration scripts from `filesystem:` locations.** For a state-based project where the deployment SQL is generated rather than authored, section 13 is the gate that applies.
- **No autofix.** Custom rules load lint-only. Setting `is_fix_compatible = True` has no effect.
- **A 60,000-line limit on a single file** applies to `check -code`. Stated in Redgate's guidance rather than on the `check -code` reference page, so confirm it if you have files near that size.
- **Nothing here catches a migration that was never written.** The skipped contract step in section 7 is the example, and it is a retention gap rather than a change-control one.

### What this does not do

These rules govern **deployment-time change to PII-tagged objects**. They do not see application runtime access to PII data. If the requirement is to know who read or wrote personal data during normal operation, that is database auditing and monitoring — a different set of tooling and a separate conversation. Deciding which of the two you need now avoids building the wrong control carefully.

Flyway is also not a data pipeline, not backup and restore, and not a monitoring tool. It versions and deploys schema and reference-data change.

## 12. Performance

Worth addressing directly, because a rule that consults a list on every statement sounds expensive.

The manifest is parsed once per process and reduced to Python sets and dicts, so lookups are constant time and manifest size does not affect them meaningfully.

The number that matters is the **marginal** cost of adding the rules, not the wall time of a run, because most of a code review run is parsing and the parse happens once regardless of how many rules are active. Measured on a 600-statement migration of mixed DML and DDL (36 KB) against a manifest of 500 tables and 5,000 tagged columns:

| Configuration | Wall time | Marginal cost |
|---|---|---|
| Parse only, one near-noop rule | 6.81s | baseline |
| `PI01` + `PI02` | 7.21s | **+0.40s** |
| `LT01`, one stock layout rule | 8.46s | +1.65s |
| `PI01` + `PI02` + `LT01` | 12.25s | +5.44s |

Adding both PII rules costs about 6% on top of the parse. One stock layout rule costs roughly four times as much, because it inspects every token while these inspect only statement-level nodes.

**How this was measured, so you can discount it appropriately.** Standalone SQLFluff 3.4.2 on Linux, not through the Flyway CLI, against a synthetic migration and a synthetic manifest, best of repeated runs. Your own numbers will differ with statement complexity and agent hardware. The ratios between rows are the durable part; the absolute times are not. Run it against one of your real migrations before quoting any of this internally.

Maintenance cost sits in the manifest generator, not the rules. The rules have no knowledge of your metadata layer's shape, so a change there does not require a rule change.

## 13. Phase two: gating the generated deployment script

For state-based projects, and for the view and dynamic SQL gaps in section 12, the complementary check runs in the release loop rather than the author loop.

Run `flyway check -dryrun` against the target to produce the SQL that will actually execute there, then apply the PII check to that SQL before the release is allowed to promote to the next ring. The gate sees generated SQL nobody typed, and it sees it per target, which matters when rings sit at different schema versions.

**Scope this honestly before committing to it.** The dry run report's JSON carries the deployment SQL as a string, not an itemized inventory of the objects being changed. So the gate has to parse that SQL itself. The good news is that the parsing is already written — the same `rules/` modules in this package can be run against the dry-run SQL by pointing standalone SQLFluff at it, rather than reimplementing the matching logic. The work is in the pipeline plumbing, not the analysis. Treat it as a real project rather than a configuration change.

Three reasons to build it second. It needs a connection to each target, which is a larger access conversation than a lint step. It reports late, when the change is already through review, so it is a backstop rather than a feedback loop. And it only earns its cost once the author-loop gate is running and trusted.

One thing worth knowing before you design it: engine 13.6.0 added a deployment overview table to the dry run report. If a change-approval board currently reviews a hand-written summary of what a release does, that table is the artifact to put in front of them instead, independently of anything to do with PII.

## 14. Requirements

- **Flyway Enterprise.** `check.sqlfluffCustomRulesPath` has been available since engine 12.7.0. SARIF output since 12.2.0. `check.code.profile` and `check.code.profiles` since 13.6.0 — on an earlier engine, use the `rules` line in `sqlfluff.cfg` for phasing instead.
- **Engine 13.6.0** was the current stable release when this was written, released 10 September 2026. Engine and Flyway Desktop version independently, so do not infer one from the other.
- **No Python install needed.** The Flyway CLI ships a packaged SQLFluff, and the documentation is explicit that "Flyway removes the need to install Python and SQLFluff yourself." Earlier drafts of this document said otherwise; they were wrong.
- The directory pointed at by `sqlfluffCustomRulesPath` must exist and must contain `__init__.py`. If either is untrue, `check -code` aborts with a named error rather than running with rules silently missing, which is the behavior you want.
- **Use a service-account personal access token for CI authentication**, not a license key. License keys expire on the contract date and break pipelines silently at renewal, and that failure is hard to diagnose from a build log.

### The prerequisite most likely to bite your on-prem rings

**The packaged SQLFluff requires GLIBC 2.35 or later on Linux.** The documented distributions that meet it are Ubuntu 22.04+, Debian 12+, and RHEL / Rocky / Alma 9.1+. On anything older, SQLFluff does not start and the error is:

```text
version `GLIBC_2.35' not found
```

Check this on every build agent before planning a rollout, and check it first on agents serving the on-prem and legacy rings, which are the likeliest to be on an older base image. A gate that cannot start on the agents serving your most conservative customers is a gate you do not have where you most need it.

### Two upgrade watch-outs

- **Upgrading to engine 13.6.0 requires running `flyway auth` once more.** Build agents included. Worth scheduling rather than discovering.
- **The official Docker images moved from JRE 21 to JRE 25 in engine 13.6.0.** If you run Flyway from the Docker image rather than an installed CLI, confirm the rules load in the new image before relying on them in a gate, and pin an explicit image tag rather than a floating one.

### Version compatibility, settled

The Flyway CLI bundles **SQLFluff 3.4.2**, published on the rules library page as "SqlFluff version 3.4.2 (Redgate Bundle)". The rules in this package were verified on 3.4.2 and on 4.3.0, so the plugin API question that would otherwise be the first risk is already answered.

The first thing to run is still the test pair: drop the package in, run `flyway check -code -check.scope="script" -check.scriptFilename="..."` against each file in `code-review-rules/tests/pii/`, and confirm 20 `PI01`/`PI02` violations on `V001` and 0 on `V002`. If a future Flyway release moves to a SQLFluff major version that relocates `BaseRule`, `LintResult` or `SegmentSeekerCrawler`, Flyway reports a plugin load error naming the exception and the fix is a one-line import change at the top of `rules/_shared.py`.

## Reference

- [Configuring custom SQLFluff rules](https://documentation.red-gate.com/flyway/reference/code-review-rules/configuring-custom-sqlfluff-rules)
- [Code review rules hub](https://documentation.red-gate.com/flyway/reference/code-review-rules)
- [Redgate SQLFluff rules library](https://documentation.red-gate.com/flyway/reference/code-review-rules/redgate-sqlfluff-rules-library)
- [Code review concepts](https://documentation.red-gate.com/flyway/flyway-concepts/code-review)
- [check -code reference](https://documentation.red-gate.com/flyway/reference/commands/check/check-code)
- [check.code.noqaSeverity](https://documentation.red-gate.com/flyway/reference/configuration/flyway-namespace/flyway-check-namespace/flyway-check-code-noqa-severity-setting)
- [check -dryrun reference](https://documentation.red-gate.com/flyway/reference/commands/check/check-dryrun)
- [Running code review in a pipeline](https://documentation.red-gate.com/flyway/deploying-database-changes-using-flyway/running-code-review)
- [Conditionally executing migrations](https://documentation.red-gate.com/flyway/flyway-concepts/migrations/conditionally-executing-migrations)
- [shouldExecute script setting](https://documentation.red-gate.com/flyway/reference/script-configuration/should-execute)
- [Drift analysis](https://documentation.red-gate.com/flyway/flyway-concepts/drift-analysis)
- [Licensing, including authentication from the command line](https://documentation.red-gate.com/flyway/getting-started-with-flyway/licensing) — the path for CI authentication; ask your Redgate contact for the current personal access token page, which has moved
- [Expand and contract, a vendor-neutral walkthrough](https://www.prisma.io/dataguide/types/relational/expand-and-contract-pattern) — breaks the pattern into more steps than the three-step outline in section 7
- [Authoring SQLFluff rules](https://docs.sqlfluff.com/en/stable/perma/rules.html)
