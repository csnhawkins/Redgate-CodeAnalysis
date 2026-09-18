# Plugin architecture and the SQLFluff rules API

## The plugin entry point

A custom rule package for Flyway needs an `__init__.py` at the root of the directory pointed at by
`check.sqlfluffCustomRulesPath`, exposing the standard SQLFluff plugin hook:

```python
"""Custom Flyway code review rules."""

from sqlfluff.core.plugin import hookimpl


@hookimpl
def get_rules():
    from .rules.<concern> import Rule_XX01, Rule_XX02
    return [Rule_XX01, Rule_XX02]
```

Two things about this file matter, and both will break the package if changed:

- **The rule imports happen inside `get_rules()`, not at module top level.** The package has to finish
  loading before SQLFluff's metaclass inspects the rule classes. Importing them at module top level
  produces a warning ("Rule has been imported before all plugins have been fully loaded") and, on some
  SQLFluff versions, a load failure.
- **There is no `get_configs_info()` hook unless a rule declares a SQLFluff config parameter.** If the
  only "configuration" your rule needs is a data file (a manifest — see `manifest-pattern.md`), resolve
  its path from the package directory or an environment variable inside the rule code itself, rather than
  plumbing it through SQLFluff's config system. Plugin config plumbing has varied between SQLFluff
  releases; keeping it out of the picture removes a class of version-dependent failure.

## The rule-code naming trap — verify before you commit to a prefix

SQLFluff validates every `BaseRule` subclass's name against a regex and extracts the rule's code from it.
The exact pattern (as of SQLFluff 3.4.2, checked directly against the installed package):

```
Rule_?([A-Z]{1}[a-zA-Z]+)?_([A-Z0-9]{4})
```

The code group is **exactly 4 alphanumeric characters**, always. That means:

- `Rule_EP01` → code `EP01` (2-letter prefix + 2-digit number — the shape that works cleanly)
- `Rule_PII01` → code `PII0` — the regex just takes the first 4 characters it can match, silently
  **truncating and discarding the trailing digit**
- `Rule_PII02` → also code `PII0` — the exact same code as the one above

That last pair is the trap: two different rule classes end up with the same 4-character code, and one of
them silently fails to register (or overwrites the other, depending on load order). Nothing raises an
error — the plugin loads "successfully" and you're missing a rule.

There's also a plugin-namespaced form SQLFluff supports for genuine multi-word prefixes: `Rule_PluginName_XXNN`,
e.g. `Rule_PII_EP01` → code `PII_EP01`. This works and is unique, but it's verbose in every report line and
in every `-- noqa:` suppression comment.

**Before choosing a prefix, verify it against the real regex rather than guessing.** This takes under a
minute:

```python
import regex
p = regex.compile(r"Rule_?([A-Z]{1}[a-zA-Z]+)?_([A-Z0-9]{4})")
for name in ["Rule_XX01", "Rule_XX02"]:
    print(name, "->", p.match(name).groups())
```

If both candidate class names produce the same code group, pick a different 2-letter prefix. The
convention that's proven to work: **exactly 2 letters + exactly 2 digits**, matching the shape of
Redgate's own `RG01`–`RG23` library and every stock SQLFluff rule.

Also confirm the prefix isn't reserved — see `requirements-and-gotchas.md`.

## No shared abstract base class

Do not create an intermediate class that inherits from `BaseRule` to share logic between your rules:

```python
# Breaks plugin loading — do not do this
class _SharedPIIRule(BaseRule):
    ...

class Rule_XX01(_SharedPIIRule):
    ...
```

SQLFluff's metaclass validates the class name of **every** `BaseRule` subclass against the `Rule_XXnn`
pattern at import time. `_SharedPIIRule` doesn't match it, so the whole plugin raises `SQLFluffUserError`
and fails to load — not just that one class. Put shared logic in module-level functions and call them from
each rule's `_eval()` method instead.

## Crawling the right statements

```python
crawl_behaviour = SegmentSeekerCrawler(
    {"insert_statement", "update_statement", "delete_statement", "merge_statement"}
)
```

Seek whole statements, not individual column or table references. Then walk each statement's own parse
tree in `_eval()` to work out what it actually touches. This is what makes the next two sections possible.

The statement segment names (`drop_table_statement`, `truncate_table`, `alter_table_statement`, etc.) are
identical across the SQL Server, PostgreSQL, Oracle and MySQL dialects, so a rule written once ports to
other dialects with no code change — only the manifest and any dialect-specific statement handling (see
below) needs to change.

## Write-target vs read-target — the distinction that decides whether the rule is usable

A compliance rule that fires on every *mention* of a protected object is useless noise. A `SELECT` that
reads a protected column in a `WHERE` clause, or a `JOIN` that reads a protected table only to look up a
foreign key, is not the same risk as writing to it. Getting this right is the single biggest factor in
whether the rule is trusted or switched off.

**Finding the actual write target:**

```python
def _target_table(statement):
    """The table the statement writes to, with aliases resolved.

    Only the first table_reference among the statement's *direct children* is
    considered — this is what keeps a protected table named in a subquery, a
    join, or a MERGE USING source from being reported as a write.
    """
    target = None
    for child in statement.segments or []:
        if child.get_type() == "table_reference":
            target = _ref_name(child)
            break
    if not target:
        return None

    qualified, bare = target
    if qualified == bare:
        # Unqualified — may be an alias declared in a FROM or JOIN clause.
        resolved = _alias_map(statement).get(bare)
        if resolved:
            return resolved
    return target
```

**Resolving aliases**, because T-SQL allows the update target to be an alias defined in a *later* FROM
clause — `UPDATE c SET c.SSN = '1' FROM dbo.Customer AS c` — and without resolving it, the rule sees only
the alias `c` and matches nothing:

```python
def _alias_map(statement):
    """{alias: (qualified, bare)} for tables named in FROM and JOIN clauses."""
    aliases = {}
    for clause in statement.recursive_crawl("from_expression_element"):
        ref, alias = None, None
        for child in clause.recursive_crawl("table_reference", "alias_expression"):
            if child.get_type() == "table_reference" and ref is None:
                ref = _ref_name(child)
            elif child.get_type() == "alias_expression":
                alias = _last_identifier(child)
        if ref and alias:
            aliases[alias] = ref
    return aliases
```

**Statement shapes worth testing explicitly**, because each one is a case a naive text-matching rule gets
wrong in a different direction:

| Shape | Correct behaviour |
|---|---|
| `UPDATE dbo.Customer SET Email = 'x' WHERE SSN = '1'` | Not reported — the protected column is read in the predicate, not written |
| `UPDATE dbo.OrderHeader SET Total = 1 WHERE OrderID IN (SELECT id FROM dbo.Employee)` | Not reported — protected table read in a subquery, write target is untagged |
| `UPDATE o SET o.Total = 1 FROM dbo.OrderHeader o JOIN dbo.Employee e ON o.eid = e.id` | Not reported — protected table joined for reading only |
| `MERGE dbo.OrderHeader AS t USING dbo.Customer AS s ON t.id = s.id WHEN MATCHED THEN UPDATE SET t.Total = 1` | Not reported — protected table is the MERGE `USING` source, not the target |
| `ALTER TABLE dbo.OrderHeader ADD CONSTRAINT fk FOREIGN KEY (cid) REFERENCES dbo.Customer (CustomerID)` | Not reported against `dbo.Customer` — it's only referenced by the FK, not structurally changed; `dbo.OrderHeader` is the table actually being altered |
| A `MERGE ... WHEN MATCHED THEN UPDATE SET t.Email = 'then delete'` | Not reported as a delete — match the statement's actual `merge_delete_clause` segment, never search the raw text for keywords, or a string literal or comment containing the words triggers a false positive |
| `DELETE FROM dbo.Customer WHERE ...` | Reported at table level — affects every column including protected ones |
| `INSERT INTO dbo.Customer VALUES (...)` with no column list | Reported at table level — same reasoning |

## Detecting copies out of the protected classification

`SELECT ... INTO` and `INSERT ... SELECT` are the quiet way protected data leaves its classification —
copying `dbo.Customer` into `dbo.CustomerCopy` puts protected values into an object nothing else knows to
watch. Check the *source* side of the copy against the manifest, and only report when the *destination* is
not itself a protected object (a copy between two protected tables stays inside the classification and
shouldn't be flagged).

## Dialect-specific statement handling: renames

Standard `ALTER TABLE ... RENAME` names its target as a normal table reference and needs no special
handling. T-SQL's `sp_rename`, by contrast, names its target in a **string literal** — there's no
`table_reference` segment to match at all, and the literal's shape varies: `'table'`, `'schema.table'`,
`'db.schema.table'`, `'table.column'`, or `'schema.table.column'`. Test every suffix combination against
both the table and column sets rather than assuming one shape. Also handle both the positional form
(`EXEC sp_rename 'dbo.Customer.SSN', 'TaxNumber', 'COLUMN'`) and the named-argument form
(`EXEC sp_rename @newname = 'Client', @objname = 'dbo.Customer'`) — the renamed object isn't always the
first literal in the statement.

This coverage is dialect-specific and does not port to other engines; `RENAME TABLE` on MySQL needs its
own handling if that dialect is in scope.
