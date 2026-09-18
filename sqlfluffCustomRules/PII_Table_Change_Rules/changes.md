# Changes

Four issues found in review and fixed. Two were gaps that let a change through
without a violation; two were the opposite, reporting something that was
actually fine. All four are covered by the test migrations now, so they stay
fixed.

The test count has gone from **18 to 20** because two new cases were added to
`V001__pii_violations.sql`. `V002__clean.sql` still expects **0**. If you have
the count assertion in a pipeline already, update the number.

---

## 1. Copying personal data into an untagged table was not reported

`SELECT ... INTO` was covered, but the more common way to copy data was not:

```sql
INSERT INTO dbo.CustomerExport (SSN) SELECT SSN FROM dbo.Customer;
```

That reported nothing. It now reports, for the same reason `SELECT ... INTO`
always did — the copy lands somewhere your metadata layer has not tagged, so the
data leaves its classification.

A copy into *another tagged table* is deliberately not reported, because the
data stays inside the classification.

## 2. The wrong table was named when a foreign key pointed at personal data

```sql
ALTER TABLE dbo.OrderHeader ADD CONSTRAINT fk FOREIGN KEY (cid)
  REFERENCES dbo.Customer (CustomerID);
```

This reported "ALTER TABLE on dbo.Customer" — but `dbo.Customer` is only being
pointed at, not changed. The table being altered is `dbo.OrderHeader`, which
holds no personal data. A reviewer would have been sent to look at the wrong
object, and the statement should not have been flagged at all.

Structural changes are now matched against the table the statement actually
changes. `DROP TABLE a, b` still correctly reports both tables.

## 3. The words "then delete" in a comment or a quoted string triggered a violation

```sql
MERGE dbo.Customer AS t USING dbo.Stage AS s ON t.id = s.id
  WHEN MATCHED THEN UPDATE SET t.Email = 'then delete';
```

This updates one non-PII column and deletes nothing, but it was reported as
affecting every column of `dbo.Customer` — purely because the phrase appeared
inside the quoted value. Any comment containing those words did the same.

The check now looks at the statement's actual delete branch rather than
searching the text, so a genuine `WHEN MATCHED THEN DELETE` is still caught and
a string or comment is not.

## 4. Renames were missed when written with named arguments

```sql
EXEC sp_rename @newname = 'Client', @objname = 'dbo.Customer';
```

Renaming a tagged table went unreported whenever the arguments were named and
`@objname` was not first — the check always read the first value it found. It
now reads whichever value belongs to `@objname`, and still handles the ordinary
positional form.

---

## Files changed

| File | What changed |
|---|---|
| `code-review-rules/rules.py` | The four fixes above. |
| `test/V001__pii_violations.sql` | Two cases added (issues 1 and 4). Now expects 20. |
| `test/V002__clean.sql` | Three cases added, covering issues 2 and 3 plus the tagged-to-tagged copy. Still expects 0. |
| `README.md` | Counts updated to 20, coverage description and example output brought in line, two entries in Limitations corrected. |

No change to `sqlfluff.cfg`, `flyway.toml.example`, `pii-objects.json` or
`__init__.py`. Rule codes `PI01` and `PI02` are unchanged, so existing
suppressions and reporting still line up.

## Verified

Run against the shipped manifest and config on SQLFluff 3.4.0: 20 violations on
`V001`, 0 on `V002`, plus 38 further cases checked by hand to confirm nothing
that used to be caught has stopped being caught. Worth re-running on 3.4.2,
the version Flyway bundles, before this goes into a pipeline.