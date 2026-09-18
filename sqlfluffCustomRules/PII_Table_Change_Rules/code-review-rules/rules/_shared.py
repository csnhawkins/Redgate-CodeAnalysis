"""Shared helpers for every custom rule module in this package.

Nothing in this file knows what "PII" or any other specific compliance concern is. It only knows how
to (a) load and validate a JSON manifest of protected objects/columns, and (b) walk a SQLFluff parse
tree to answer generic questions like "what table does this statement write to?" or "which columns
does this UPDATE actually set?". Every domain-specific rule module (rules/pii.py today, maybe
rules/financial.py or rules/naming.py later) imports from here rather than re-implementing this logic.

Keeping this split is the whole point of the package's design: adding a new compliance concern means
writing a new rules/<concern>.py plus a new manifests/<concern>.json, never touching this file. See
code-review-rules/README.md for the step-by-step guide to adding a new rule module.

Background reading, if any of this needs debugging:
- SQLFluff plugin development guide: https://docs.sqlfluff.com/en/stable/perma/plugin_dev.html
- SQLFluff rule authoring reference: https://docs.sqlfluff.com/en/stable/perma/rules.html
- SQLFluff parse-tree segment types are dialect-specific in general, but the statement-level segment
  names used across this package (table_reference, update_statement, delete_statement, etc.) are the
  same across the SQL Server, PostgreSQL, Oracle and MySQL dialects, which is why a rule written once
  against this file's helpers ports across dialects with only the manifest changing.
"""

import json
import os
import re
import threading

# ---------------------------------------------------------------------------
# Manifest loading and validation
# ---------------------------------------------------------------------------
#
# A "manifest" is the JSON file listing which schema/table/column combinations a rule should treat as
# protected (e.g. manifests/pii.json). The manifest is the ONLY thing that should change often; the
# rule code that reads it should not need to change when the list of protected objects does.
#
# Design choices worth knowing about if you're extending this:
#
# - Manifests are cached per resolved file path, for the lifetime of the SQLFluff/Flyway process. A
#   large migration can trigger this rule dozens of times in one lint run, and re-parsing the same
#   JSON file on every statement would be wasteful. See load_manifest() below.
# - A manifest that fails to parse or fails validation is NOT cached, and every _eval() call re-tries
#   loading it. That's deliberate: a transient read error (e.g. the file is being rewritten by a
#   pipeline step at the exact moment a lint run starts) shouldn't permanently poison the rest of the
#   process. Only a manifest that parses AND validates cleanly gets cached.
# - Validation is strict on purpose. A manifest that "half loads" (e.g. one bad entry silently
#   skipped) is more dangerous than one that fails outright, because it silently narrows what's
#   protected without telling anyone. See _parse_manifest() below for the specific checks.

# _ROOT_DIR is the code-review-rules/ package root (the parent of this rules/ subfolder), which is
# where the manifests/ folder lives. Computed once at import time rather than hardcoded, so the
# package still works if it's checked out under a different top-level folder name (e.g. as a git
# submodule with a different local path).
_ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

_CACHE = {}
_CACHE_LOCK = threading.Lock()

# Strips SQL Server [bracket] quoting, ANSI "double quotes", and MySQL/backtick quoting from an
# identifier, so `[dbo].[Customer]`, `dbo.Customer` and `DBO.CUSTOMER` all normalize to the same
# lookup key. This is deliberately permissive across dialects (SQLFluff already knows which quoting
# style is valid for the active dialect, so any manifest lookup that reaches this point has already
# been parsed as a valid identifier in that dialect).
_QUOTES = re.compile(r'[\[\]"`]')


class Manifest:
    """A parsed, validated manifest of protected schema/table/column combinations.

    If `error` is set, the manifest failed to load or failed validation, and the rule that requested
    it should report that error as a lint violation rather than silently treating the object list as
    empty. A gate that goes quiet because its manifest broke is worse than no gate at all, because a
    clean lint result and a broken manifest look identical unless the rule speaks up.
    """

    __slots__ = ("qualified", "bare", "by_table", "all_columns", "display", "error")

    def __init__(self, error=None):
        self.qualified = set()      # {"dbo.customer", ...} - schema-qualified lookup keys
        self.bare = set()           # {"customer", ...} - unqualified lookup keys
        self.by_table = {}          # lookup key -> {"ssn", ...} or {"*"} for a whole-table tag
        self.all_columns = set()    # every tagged column name, across all tables, for sp_rename etc.
        self.display = {}           # normalized key -> original casing, for readable violation text
        self.error = error

    def lookup(self, qualified, bare):
        """Return (matched_key, protected_columns) for a table reference, or None if it isn't tagged.

        Tries the schema-qualified form first, then falls back to the bare table name. The bare-name
        fallback is what makes an unqualified `UPDATE Customer SET ...` match a manifest entry for
        `dbo.Customer` - which is also why an unqualified reference over-reports if two different
        schemas both have a table with that name and only one is tagged. That's the safe direction for
        a compliance control (a false positive gets reviewed and dismissed; a false negative doesn't
        get reviewed at all), so it's left as-is rather than "fixed" - the real fix is enforcing
        schema-qualified names via a separate naming-convention rule.
        """
        if qualified in self.qualified:
            return qualified, self.by_table.get(qualified, set())
        if bare in self.bare:
            return bare, self.by_table.get(bare, set())
        return None

    def shown(self, key):
        """The original-cased form of a normalized key, for use in violation messages."""
        return self.display.get(key, key)

    def names(self, keys):
        """A sorted, comma-joined, original-cased list of column names, for violation messages."""
        return ", ".join(self.display.get(k, k) for k in sorted(keys))


def normalize_identifier(raw):
    """Strip identifier quoting of any flavor and case-fold, for consistent manifest lookups."""
    if not isinstance(raw, str):
        return ""
    return _QUOTES.sub("", raw).strip().lower()


def _parse_manifest(data):
    """Build a Manifest from already-decoded JSON, validating its shape strictly.

    Every shape problem returns a Manifest with a specific, human-readable `error` describing exactly
    what's wrong and where (e.g. "objects[2] 'columns' must be a JSON array, got str"). Nothing here
    is a warning-and-continue - a manifest that doesn't validate cleanly is treated as fully unusable,
    because a partially-applied manifest silently protects less than the file suggests it protects.

    The empty-columns check is the case worth remembering: `"columns": []` is REJECTED rather than
    treated as "nothing tagged here" or "everything tagged here" - it's ambiguous, and ambiguous is
    not a state a compliance control should guess its way through. Use `["*"]` to explicitly tag a
    whole table, or omit the entry from `objects` entirely.
    """
    if not isinstance(data, dict):
        return Manifest(error="manifest root must be a JSON object")

    objects = data.get("objects")
    if not isinstance(objects, list):
        return Manifest(error="'objects' must be a JSON array")
    if not objects:
        return Manifest(error="'objects' is empty - nothing would be protected")

    m = Manifest()
    for i, entry in enumerate(objects):
        where = f"objects[{i}]"
        if not isinstance(entry, dict):
            return Manifest(error=f"{where} must be a JSON object")

        raw_schema = entry.get("schema", "")
        raw_table = entry.get("table")
        if not isinstance(raw_table, str) or not raw_table.strip():
            return Manifest(error=f"{where} 'table' must be a non-empty string")
        if not isinstance(raw_schema, str):
            return Manifest(error=f"{where} 'schema' must be a string")

        cols = entry.get("columns")
        if not isinstance(cols, list):
            return Manifest(
                error=f"{where} 'columns' must be a JSON array, got "
                f"{type(cols).__name__}"
            )
        if not cols:
            return Manifest(
                error=f"{where} 'columns' is empty. Use [\"*\"] to tag the "
                f"whole table, or omit the entry."
            )
        for c in cols:
            if not isinstance(c, str) or not c.strip():
                return Manifest(
                    error=f"{where} 'columns' must contain non-empty strings"
                )

        schema = normalize_identifier(raw_schema)
        table = normalize_identifier(raw_table)
        key = f"{schema}.{table}" if schema else table
        normalized_cols = {normalize_identifier(c) for c in cols}

        m.qualified.add(key)
        m.bare.add(table)
        m.by_table.setdefault(key, set()).update(normalized_cols)
        m.by_table.setdefault(table, set()).update(normalized_cols)
        m.all_columns.update(c for c in normalized_cols if c != "*")

        raw_key = f"{raw_schema}.{raw_table}" if raw_schema else raw_table
        m.display[key] = raw_key
        m.display.setdefault(table, raw_key)
        for c in cols:
            m.display.setdefault(normalize_identifier(c), c)

    return m


def load_manifest(filename, env_var):
    """Load and cache the manifest for one compliance concern.

    filename  - the default manifest, relative to the manifests/ folder next to this rules/ folder,
                e.g. "pii.json" resolves to code-review-rules/manifests/pii.json.
    env_var   - the name of an environment variable that overrides the default path entirely, for
                pipelines that generate a fresh manifest at the start of each run rather than reading
                a file committed to the repo (see docs/pii.md for when that trade-off makes sense).
                An absolute path in the env var is used as-is; a relative one is resolved against the
                code-review-rules/ package root, not the current working directory, so it behaves the
                same whether SQLFluff is invoked from the repo root or from somewhere else in CI.

    Call this once per rule module with that module's own filename/env var pair - see rules/pii.py
    for the calling pattern. Each distinct resolved path gets its own cache entry, so multiple
    concerns with different manifests don't collide.
    """
    path = os.environ.get(env_var) or os.path.join("manifests", filename)
    resolved = path if os.path.isabs(path) else os.path.join(_ROOT_DIR, path)

    with _CACHE_LOCK:
        cached = _CACHE.get(resolved)
        if cached is not None and cached.error is None:
            return cached
        try:
            with open(resolved, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except OSError as exc:
            return Manifest(error=f"cannot read {resolved}: {exc}")
        except ValueError as exc:
            return Manifest(error=f"{resolved} is not valid JSON: {exc}")
        except Exception as exc:  # noqa - never crash the lint run over a bad manifest
            return Manifest(error=f"{resolved} could not be loaded: {exc}")

        try:
            m = _parse_manifest(data)
        except Exception as exc:  # noqa - never crash the lint run over a bad manifest
            m = Manifest(error=f"{resolved} could not be parsed: {exc}")

        if m.error is None:
            _CACHE[resolved] = m
        return m


# ---------------------------------------------------------------------------
# Parse-tree helpers
# ---------------------------------------------------------------------------
#
# These answer generic structural questions about a statement's own parse tree - "what table does
# this write to", "which columns does it set", "does it affect every column" - without knowing
# anything about which objects are protected. A rule module combines these with a Manifest lookup.

def descendants(segment, types):
    """Every descendant segment of the given type(s), in document order, excluding `segment` itself.

    Document order matters more than it looks like it should: T-SQL's `sp_rename` identifies its
    target in its FIRST string argument, so a reversed or unordered traversal can pick up the second
    argument ('COLUMN', or a new name) instead of the actual target. `recursive_crawl` (SQLFluff's own
    parse-tree walk) is used here rather than a hand-rolled stack specifically to guarantee that order.
    """
    return [seg for seg in segment.recursive_crawl(*types) if seg is not segment]


def ref_name(ref):
    """(qualified, bare) normalized names for a table or object reference segment.

    Returns None if the reference segment has no identifiable name (shouldn't normally happen for a
    valid parse, but defensive because a rule crashing mid-lint-run is worse than it under-reporting
    one statement).
    """
    parts = [
        normalize_identifier(p.raw)
        for p in ref.recursive_crawl("naked_identifier", "quoted_identifier")
    ]
    parts = [p for p in parts if p]
    if not parts:
        return None
    bare = parts[-1]
    qualified = ".".join(parts[-2:]) if len(parts) >= 2 else bare
    return qualified, bare


def alias_map(statement):
    """{alias: (qualified, bare)} for every table named in this statement's FROM/JOIN clauses.

    T-SQL allows an UPDATE/DELETE's write target to be an alias declared in a LATER from_expression,
    e.g. `UPDATE c SET c.SSN = '1' FROM dbo.Customer AS c`. Without resolving `c` back to
    `dbo.Customer` here, target_table() below would see only the alias and match nothing in the
    manifest - silently under-reporting a real write to a protected column.
    """
    aliases = {}
    for clause in descendants(statement, {"from_expression_element"}):
        ref, alias = None, None
        for child in clause.recursive_crawl("table_reference", "alias_expression"):
            if child.get_type() == "table_reference" and ref is None:
                ref = ref_name(child)
            elif child.get_type() == "alias_expression":
                names = [
                    normalize_identifier(p.raw)
                    for p in child.recursive_crawl(
                        "naked_identifier", "quoted_identifier"
                    )
                ]
                names = [n for n in names if n]
                if names:
                    alias = names[-1]
        if ref and alias:
            aliases[alias] = ref
    return aliases


def target_table(statement):
    """The table this statement actually writes to, with aliases resolved.

    Deliberately only looks at the FIRST table_reference among the statement's DIRECT children (not
    nested inside a subquery, JOIN condition, or MERGE USING clause). That's what keeps a protected
    table named only in a subquery, a join, or a MERGE's USING source from being mistaken for a write
    target - in all of those cases the table is being READ to locate rows, not written to, and a
    compliance rule that can't tell the difference generates so much noise that it gets disabled.

    Verified (see the shipped test/tests migrations) against UPDATE, INSERT, DELETE (including the
    T-SQL two-FROM DELETE form) and MERGE, across the alias and no-alias forms of each.
    """
    target = None
    for child in statement.segments or []:
        if child.get_type() == "table_reference":
            target = ref_name(child)
            break
    if not target:
        return None

    qualified, bare = target
    if qualified == bare:
        # Unqualified name - it might actually be an alias declared in a FROM/JOIN clause rather than
        # a real table name (see alias_map() above for why this specific case matters).
        resolved = alias_map(statement).get(bare)
        if resolved:
            return resolved
    return target


def all_tables(statement):
    """Every table this statement references anywhere in its parse tree - reads AND writes."""
    out = []
    for ref in descendants(statement, {"table_reference"}):
        name = ref_name(ref)
        if name:
            out.append(name)
    return out


def direct_tables(statement):
    """Tables named as a DIRECT child of the statement, not inside a nested clause.

    DROP, TRUNCATE and ALTER all name their real target as a direct child - so a table that's merely
    REFERENCED (the target of a foreign key added by an ALTER TABLE ... ADD CONSTRAINT, for example)
    is correctly excluded here, because that referenced table isn't itself being structurally changed.
    """
    out = []
    for child in statement.segments or []:
        if child.get_type() == "table_reference":
            name = ref_name(child)
            if name:
                out.append(name)
    return out


def source_tables(statement):
    """Tables this statement reads from, excluding whichever table(s) it writes to directly."""
    written = {id(c) for c in statement.segments or []
               if c.get_type() == "table_reference"}
    out = []
    for ref in descendants(statement, {"table_reference"}):
        if id(ref) in written:
            continue
        name = ref_name(ref)
        if name:
            out.append(name)
    return out


def column_names(segment):
    """Bare (unqualified) column names referenced anywhere under the given segment."""
    return {
        normalize_identifier(c.raw.split(".")[-1])
        for c in descendants(segment, {"column_reference"})
    }


def written_columns(statement):
    """Bare names of columns this statement actually WRITES (not merely reads).

    UPDATE   - columns inside SET clauses only. A column used purely in a WHERE predicate is a read,
               not a write, and must not appear here.
    INSERT   - the explicit column list, when there is one. (No column list means every column is
               written - see affects_whole_row() below, which is a different, table-level check.)
    MERGE    - SET clauses plus the column list of the WHEN NOT MATCHED INSERT branch, since a MERGE
               can write via either branch.
    DELETE   - none. A DELETE doesn't "write columns" in this sense - it's a whole-row operation,
               handled by affects_whole_row() instead.
    """
    stype = statement.get_type()
    cols = set()

    if stype in ("update_statement", "merge_statement"):
        for set_list in descendants(statement, {"set_clause_list"}):
            cols |= column_names(set_list)

    if stype == "merge_statement":
        # The INSERT branch of a MERGE writes columns that never appear in a set_clause_list - this
        # is the standard bulk-load/upsert shape (`WHEN NOT MATCHED THEN INSERT (...) VALUES (...)`).
        for clause in descendants(statement, {"merge_when_not_matched_clause"}):
            for br in descendants(clause, {"bracketed"}):
                found = column_names(br)
                if found:
                    cols |= found
                    break

    if stype == "insert_statement":
        for child in statement.segments or []:
            if child.get_type() == "bracketed":
                cols |= column_names(child)
                break

    return cols


def affects_whole_row(statement):
    """True when this statement affects every column of its target table, not just some of them.

    A DELETE removes the whole row. An INSERT with no explicit column list writes every column. A
    MERGE's DELETE branch (matched via the merge_delete_clause segment specifically, never by
    searching the raw statement text) also removes the whole row. Matching the actual parse-tree
    segment rather than searching for the word "delete" is deliberate: a string literal or SQL comment
    containing the words "then delete" must NOT trigger this, and only inspecting the real
    merge_delete_clause segment guarantees that.
    """
    stype = statement.get_type()
    if stype == "delete_statement":
        return True
    if stype == "merge_statement":
        if descendants(statement, {"merge_delete_clause"}):
            return True
    if stype == "insert_statement":
        return not any(
            c.get_type() == "bracketed" and column_names(c)
            for c in statement.segments or []
        )
    return False
