"""PI01 / PI02 - PII-aware code review rules for Flyway check -code.

PI01  DML that writes a PII-tagged column, affects every column of a
      PII-tagged table, or copies a PII-tagged table into another table.
PI02  Structural change (DROP / TRUNCATE / ALTER / sp_rename) to a table that
      holds PII-tagged columns.

The PII object list is loaded from a JSON manifest generated upstream from the
metadata service. The manifest is parsed once per process and cached.

Not from Redgate docs. Verified against SQLFluff 3.4.2 (the version Flyway
bundles) and 4.3.0.
"""

import json
import os
import re
import threading

from sqlfluff.core.rules import BaseRule, LintResult
from sqlfluff.core.rules.crawlers import SegmentSeekerCrawler

# ---------------------------------------------------------------------------
# Manifest loading and validation
# ---------------------------------------------------------------------------

_CACHE = {}
_CACHE_LOCK = threading.Lock()
_PACKAGE_DIR = os.path.dirname(os.path.abspath(__file__))
_QUOTES = re.compile(r'[\[\]"`]')


class Manifest:
    """Parsed PII manifest. `error` is set if the file could not be used."""

    __slots__ = ("qualified", "bare", "by_table", "all_columns", "display", "error")

    def __init__(self, error=None):
        self.qualified = set()      # {"dbo.customer", ...}
        self.bare = set()           # {"customer", ...}
        self.by_table = {}          # key -> {"ssn", ...} or {"*"}
        self.all_columns = set()    # {"ssn", ...}
        self.display = {}           # normalized -> original casing
        self.error = error

    def lookup(self, qualified, bare):
        """Return (match_key, pii_columns) or None."""
        if qualified in self.qualified:
            return qualified, self.by_table.get(qualified, set())
        if bare in self.bare:
            return bare, self.by_table.get(bare, set())
        return None

    def shown(self, key):
        return self.display.get(key, key)

    def names(self, keys):
        return ", ".join(self.display.get(k, k) for k in sorted(keys))


def _normalize(raw):
    """Strip identifier quoting of any flavor and case-fold."""
    if not isinstance(raw, str):
        return ""
    return _QUOTES.sub("", raw).strip().lower()


def _parse_manifest(data):
    """Build a Manifest from already-decoded JSON, validating shape.

    Shape errors are fatal and reported, never skipped. A manifest that
    half-loads is worse than one that fails, because it silently narrows
    what is protected.
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

        schema = _normalize(raw_schema)
        table = _normalize(raw_table)
        key = f"{schema}.{table}" if schema else table
        normalized_cols = {_normalize(c) for c in cols}

        m.qualified.add(key)
        m.bare.add(table)
        m.by_table.setdefault(key, set()).update(normalized_cols)
        m.by_table.setdefault(table, set()).update(normalized_cols)
        m.all_columns.update(c for c in normalized_cols if c != "*")

        raw_key = f"{raw_schema}.{raw_table}" if raw_schema else raw_table
        m.display[key] = raw_key
        m.display.setdefault(table, raw_key)
        for c in cols:
            m.display.setdefault(_normalize(c), c)

    return m


def _load_manifest():
    path = os.environ.get("PII_MANIFEST_PATH") or "pii-objects.json"
    resolved = path if os.path.isabs(path) else os.path.join(_PACKAGE_DIR, path)

    with _CACHE_LOCK:
        cached = _CACHE.get(resolved)
        # A previous failure is not cached, so a transient read error does not
        # poison the gate for the rest of the process.
        if cached is not None and cached.error is None:
            return cached
        try:
            with open(resolved, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except OSError as exc:
            return Manifest(error=f"cannot read {resolved}: {exc}")
        except ValueError as exc:
            return Manifest(error=f"{resolved} is not valid JSON: {exc}")
        except Exception as exc:  # noqa - never crash the lint run
            return Manifest(error=f"{resolved} could not be loaded: {exc}")

        try:
            m = _parse_manifest(data)
        except Exception as exc:  # noqa - never crash the lint run
            m = Manifest(error=f"{resolved} could not be parsed: {exc}")

        if m.error is None:
            _CACHE[resolved] = m
        return m


# ---------------------------------------------------------------------------
# Parse-tree helpers
# ---------------------------------------------------------------------------

def _descendants(segment, types):
    """Matching descendants in document order.

    Document order matters: sp_rename identifies its target in the first
    string argument, so a reversed traversal picks up 'COLUMN' instead.
    recursive_crawl is used rather than a hand-rolled stack for exactly
    that reason.
    """
    return [
        seg for seg in segment.recursive_crawl(*types) if seg is not segment
    ]


def _ref_name(ref):
    """(qualified, bare) for a table or object reference segment."""
    parts = [
        _normalize(p.raw)
        for p in ref.recursive_crawl("naked_identifier", "quoted_identifier")
    ]
    parts = [p for p in parts if p]
    if not parts:
        return None
    bare = parts[-1]
    qualified = ".".join(parts[-2:]) if len(parts) >= 2 else bare
    return qualified, bare


def _alias_map(statement):
    """{alias: (qualified, bare)} for tables named in FROM and JOIN clauses.

    T-SQL allows the update target to be an alias defined in a later FROM
    clause, as in `UPDATE c SET c.SSN = '1' FROM dbo.Customer AS c`.
    Without this the target resolves to 'c' and matches nothing.
    """
    aliases = {}
    for clause in _descendants(statement, {"from_expression_element"}):
        ref, alias = None, None
        for child in clause.recursive_crawl("table_reference", "alias_expression"):
            if child.get_type() == "table_reference" and ref is None:
                ref = _ref_name(child)
            elif child.get_type() == "alias_expression":
                names = [
                    _normalize(p.raw)
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


def _target_table(statement):
    """The table the statement writes to, with aliases resolved.

    Only the first table_reference among the statement's direct children is
    considered. This is what keeps a PII table named in a subquery, a join, or
    a MERGE USING source from being reported as a write. Verified on tsql for
    UPDATE, INSERT, DELETE (including the T-SQL two-FROM form) and MERGE.
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
        # Unqualified, so it may be an alias declared in a FROM or JOIN.
        resolved = _alias_map(statement).get(bare)
        if resolved:
            return resolved
    return target


def _all_tables(statement):
    out = []
    for ref in _descendants(statement, {"table_reference"}):
        name = _ref_name(ref)
        if name:
            out.append(name)
    return out


def _direct_tables(statement):
    """Tables the statement names directly, not inside a nested clause.

    DROP, TRUNCATE and ALTER all name their target as a direct child, so a
    table that is merely referenced - the REFERENCES target of a foreign key
    added by ALTER TABLE, for example - is excluded.
    """
    out = []
    for child in statement.segments or []:
        if child.get_type() == "table_reference":
            name = _ref_name(child)
            if name:
                out.append(name)
    return out


def _source_tables(statement):
    """Tables the statement reads, excluding the table it writes to."""
    written = {id(c) for c in statement.segments or []
               if c.get_type() == "table_reference"}
    out = []
    for ref in _descendants(statement, {"table_reference"}):
        if id(ref) in written:
            continue
        name = _ref_name(ref)
        if name:
            out.append(name)
    return out


def _column_names(segment):
    return {
        _normalize(c.raw.split(".")[-1])
        for c in _descendants(segment, {"column_reference"})
    }


def _written_columns(statement):
    """Bare names of columns the statement writes.

    UPDATE   - columns inside SET clauses only, so a PII column used purely in
               a WHERE predicate is not treated as a write.
    INSERT   - the explicit column list, when there is one.
    MERGE    - SET clauses plus the column list of the WHEN NOT MATCHED INSERT.
    DELETE   - none; handled at table level.
    """
    stype = statement.get_type()
    cols = set()

    if stype in ("update_statement", "merge_statement"):
        for set_list in _descendants(statement, {"set_clause_list"}):
            cols |= _column_names(set_list)

    if stype == "merge_statement":
        # The INSERT branch of a MERGE writes columns that never appear in a
        # set_clause_list. This is the standard bulk-load shape.
        for clause in _descendants(statement, {"merge_when_not_matched_clause"}):
            for br in _descendants(clause, {"bracketed"}):
                found = _column_names(br)
                if found:
                    cols |= found
                    break

    if stype == "insert_statement":
        for child in statement.segments or []:
            if child.get_type() == "bracketed":
                cols |= _column_names(child)
                break

    return cols


def _affects_whole_row(statement):
    """True when the statement affects every column of its target table."""
    stype = statement.get_type()
    if stype == "delete_statement":
        return True
    if stype == "merge_statement":
        # The DELETE branch is its own segment. Matching on that rather than
        # the raw text avoids firing on the words "then delete" inside a
        # string literal or a comment.
        if _descendants(statement, {"merge_delete_clause"}):
            return True
    if stype == "insert_statement":
        # No explicit column list means every column is written.
        return not any(
            c.get_type() == "bracketed" and _column_names(c)
            for c in statement.segments or []
        )
    return False


# ---------------------------------------------------------------------------
# PI01 - DML against PII
# ---------------------------------------------------------------------------

class Rule_PI01(BaseRule):
    """DML against a PII-tagged column requires compliance sign-off."""

    groups = ("all", "pii")
    name = "pii.dml"
    crawl_behaviour = SegmentSeekerCrawler(
        {
            "insert_statement",
            "update_statement",
            "delete_statement",
            "merge_statement",
            "select_statement",
        }
    )
    is_fix_compatible = False

    def _eval(self, context):
        m = _load_manifest()
        if m.error:
            return LintResult(
                anchor=context.segment,
                description=f"PII manifest unusable, gate not enforced: {m.error}",
            )

        stmt = context.segment
        stype = stmt.get_type()

        if stype == "select_statement":
            return self._eval_select_into(stmt, m)

        target = _target_table(stmt)
        if not target:
            return None
        hit = m.lookup(*target)
        if not hit:
            if stype == "insert_statement":
                return self._eval_insert_copy(stmt, m)
            return None

        key, pii_cols = hit
        shown = m.shown(key)
        verb = stype.replace("_statement", "").upper()

        if "*" in pii_cols:
            return LintResult(
                anchor=stmt,
                description=(
                    f"{verb} on {shown}, which is tagged as holding personal "
                    f"data. Compliance sign-off required."
                ),
            )

        if _affects_whole_row(stmt):
            return LintResult(
                anchor=stmt,
                description=(
                    f"{verb} affects every column of {shown}, including "
                    f"PII-tagged {m.names(pii_cols)}. Compliance sign-off "
                    f"required."
                ),
            )

        overlap = _written_columns(stmt) & pii_cols
        if overlap:
            return LintResult(
                anchor=stmt,
                description=(
                    f"{verb} writes PII-tagged column(s) {m.names(overlap)} "
                    f"on {shown}. Compliance sign-off required."
                ),
            )
        return None

    @staticmethod
    def _eval_select_into(stmt, m):
        """SELECT ... INTO copies rows into a new table.

        Reported when the source side holds PII, because the copy lands in a
        table the metadata layer has not tagged, which moves personal data
        outside the classification.
        """
        into = _descendants(stmt, {"into_table_clause"})
        if not into:
            return None

        dest = None
        for ref in _descendants(into[0], {"object_reference", "table_reference"}):
            dest = ref.raw.strip()  # original casing, for a readable message
            break

        results = []
        for name in _all_tables(stmt):
            hit = m.lookup(*name)
            if not hit:
                continue
            key, pii_cols = hit
            detail = (
                "all columns" if "*" in pii_cols else m.names(pii_cols)
            )
            dest_shown = dest or "another table"
            results.append(
                LintResult(
                    anchor=stmt,
                    description=(
                        f"SELECT INTO copies {m.shown(key)} (PII-tagged "
                        f"{detail}) into {dest_shown}. The copy is not covered "
                        f"by the PII classification. Compliance sign-off "
                        f"required."
                    ),
                )
            )
        return results or None

    @staticmethod
    def _eval_insert_copy(stmt, m):
        """INSERT ... SELECT that reads PII into an untagged table.

        The same concern as SELECT ... INTO: the rows land in a table the
        metadata layer has not tagged, so they leave the classification.
        Only reported when the destination is untagged, because a copy into
        another tagged table stays inside it.
        """
        dest = "another table"
        for child in stmt.segments or []:
            if child.get_type() == "table_reference":
                dest = child.raw.strip()
                break

        results = []
        seen = set()
        for name in _source_tables(stmt):
            hit = m.lookup(*name)
            if not hit or hit[0] in seen:
                continue
            seen.add(hit[0])
            key, pii_cols = hit
            detail = "all columns" if "*" in pii_cols else m.names(pii_cols)
            results.append(
                LintResult(
                    anchor=stmt,
                    description=(
                        f"INSERT copies {m.shown(key)} (PII-tagged {detail}) "
                        f"into {dest}. The copy is not covered by the PII "
                        f"classification. Compliance sign-off required."
                    ),
                )
            )
        return results or None


# ---------------------------------------------------------------------------
# PI02 - structural change against PII
# ---------------------------------------------------------------------------

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

    def _eval(self, context):
        m = _load_manifest()
        if m.error:
            return LintResult(
                anchor=context.segment,
                description=f"PII manifest unusable, gate not enforced: {m.error}",
            )

        stmt = context.segment
        stype = stmt.get_type()

        if stype == "execute_script_statement":
            return self._eval_sp_rename(stmt, m)

        results = []
        seen = set()
        for name in _direct_tables(stmt):
            hit = m.lookup(*name)
            if not hit or hit[0] in seen:
                continue
            seen.add(hit[0])
            key, pii_cols = hit
            detail = "all columns" if "*" in pii_cols else m.names(pii_cols)
            verb = stype.replace("_statement", "").replace("_", " ").upper()
            results.append(
                LintResult(
                    anchor=stmt,
                    description=(
                        f"{verb} on {m.shown(key)}, which holds PII-tagged "
                        f"{detail}. Compliance sign-off required."
                    ),
                )
            )
        return results or None

    @staticmethod
    def _eval_sp_rename(stmt, m):
        """sp_rename passes its target as a string literal.

        There is no table_reference to match, and the literal can be
        'table', 'schema.table', 'db.schema.table', 'table.column' or
        'schema.table.column'. Rather than guess which, every suffix
        combination is tested against both the table and column sets.
        """
        if "sp_rename" not in stmt.raw.lower():
            return None

        literals = _descendants(stmt, {"quoted_literal"})
        if not literals:
            return None

        # Positionally the renamed object is the first argument, but a named
        # call can put @objname anywhere in the list.
        target_literal = literals[0]
        args = _descendants(stmt, {"parameter", "quoted_literal"})
        for i, seg in enumerate(args):
            if seg.get_type() == "parameter" and _normalize(seg.raw) == "@objname":
                named = [s for s in args[i + 1:]
                         if s.get_type() == "quoted_literal"]
                if named:
                    target_literal = named[0]
                break

        raw = target_literal.raw.strip("'\"")
        parts = [_normalize(p) for p in raw.split(".") if p.strip()]
        if not parts:
            return None

        # Any trailing pair or single could be schema.table or table.
        candidates = set()
        if len(parts) >= 2:
            candidates.add(".".join(parts[-2:]))
        candidates.add(parts[-1])
        if len(parts) >= 3:
            candidates.add(".".join(parts[-3:-1]))
        if len(parts) >= 2:
            candidates.add(parts[-2])

        for cand in candidates:
            bare = cand.split(".")[-1]
            hit = m.lookup(cand, bare)
            if hit:
                key, pii_cols = hit
                detail = "all columns" if "*" in pii_cols else m.names(pii_cols)
                return LintResult(
                    anchor=stmt,
                    description=(
                        f"sp_rename targets '{raw}'. {m.shown(key)} holds "
                        f"PII-tagged {detail}, and renaming a tagged object "
                        f"breaks the link to its classification. Compliance "
                        f"sign-off required."
                    ),
                )

        # The last part may be a tagged column name on an untagged table.
        if parts[-1] in m.all_columns:
            return LintResult(
                anchor=stmt,
                description=(
                    f"sp_rename targets '{raw}', and '{m.shown(parts[-1])}' "
                    f"matches a PII-tagged column name. Renaming a tagged "
                    f"object breaks the link to its classification. "
                    f"Compliance sign-off required."
                ),
            )
        return None
