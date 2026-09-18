"""PI01 / PI02 - PII-aware code review rules for Flyway check -code.

PI01  DML that writes a PII-tagged column, affects every column of a PII-tagged table, or copies a
      PII-tagged table into an untagged one.
PI02  Structural change (DROP / TRUNCATE / ALTER / sp_rename) to a table that holds PII-tagged
      columns.

What this file does NOT contain: any SQLFluff plumbing beyond the two rule classes themselves, and no
knowledge of *which* objects are tagged. Parse-tree walking and manifest handling both live in
rules/_shared.py, and the actual list of protected schema/table/column combinations lives in
manifests/pii.json (or wherever PII_MANIFEST_PATH points). See docs/pii.md for the plain-English
explanation of what these two rules do and why, and code-review-rules/README.md for how to add a
similar rule module for a different compliance concern.

Not from Redgate docs. Verified against SQLFluff 3.4.2 (the version Flyway bundles) and 4.3.0.
Reference: SQLFluff rule authoring guide - https://docs.sqlfluff.com/en/stable/perma/rules.html
"""

from sqlfluff.core.rules import BaseRule, LintResult
from sqlfluff.core.rules.crawlers import SegmentSeekerCrawler

from . import _shared

# The manifest filename and its environment-variable override live here, next to the rules that use
# them, rather than in _shared.py - that keeps _shared.py fully generic and makes it obvious, from
# this file alone, which manifest and which override variable PI01/PI02 read from.
_MANIFEST_FILENAME = "pii.json"
_MANIFEST_ENV_VAR = "PII_MANIFEST_PATH"


def _load_manifest():
    return _shared.load_manifest(_MANIFEST_FILENAME, _MANIFEST_ENV_VAR)


# ---------------------------------------------------------------------------
# PI01 - DML against PII
# ---------------------------------------------------------------------------

class Rule_PI01(BaseRule):
    """DML against a PII-tagged column requires compliance sign-off."""

    # "all" is the group every SQLFluff rule belongs to by default; "pii" is a custom group specific
    # to this package, letting `sqlfluff.cfg` (or a future review profile) target both PI01 and PI02
    # together as "the PII rules" without listing their codes individually.
    groups = ("all", "pii")
    name = "pii.dml"
    # SegmentSeekerCrawler visits every matching statement in the file, however deeply nested (e.g.
    # inside a stored-procedure body, if your dialect parses one). See _shared.py's module docstring
    # for why these statement segment names are dialect-portable.
    crawl_behaviour = SegmentSeekerCrawler(
        {
            "insert_statement",
            "update_statement",
            "delete_statement",
            "merge_statement",
            "select_statement",
        }
    )
    # Custom rules loaded via a plugin are lint-only - SQLFluff's autofix machinery doesn't apply to
    # them, so this must stay False. Setting it True has no effect but is worth stating explicitly so
    # nobody wonders whether autofix was meant to work here.
    is_fix_compatible = False

    def _eval(self, context):
        m = _load_manifest()
        if m.error:
            # Report the manifest failure itself as the violation, rather than returning None. A
            # rule that goes quiet because its data file broke is indistinguishable, from the build
            # log, from a genuinely clean migration - reporting the failure is what makes a broken
            # gate visible instead of silently non-enforcing.
            return LintResult(
                anchor=context.segment,
                description=f"PII manifest unusable, gate not enforced: {m.error}",
            )

        stmt = context.segment
        stype = stmt.get_type()

        if stype == "select_statement":
            return self._eval_select_into(stmt, m)

        target = _shared.target_table(stmt)
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

        if _shared.affects_whole_row(stmt):
            return LintResult(
                anchor=stmt,
                description=(
                    f"{verb} affects every column of {shown}, including "
                    f"PII-tagged {m.names(pii_cols)}. Compliance sign-off "
                    f"required."
                ),
            )

        overlap = _shared.written_columns(stmt) & pii_cols
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
        """SELECT ... INTO copies rows into a brand new table.

        Reported when the SOURCE side holds PII, because the copy lands in a table the manifest has
        never heard of, which moves personal data outside the classification entirely - the same
        underlying concern as _eval_insert_copy() below, just for the other SQL shape that does it.
        """
        into = _shared.descendants(stmt, {"into_table_clause"})
        if not into:
            return None

        dest = None
        for ref in _shared.descendants(into[0], {"object_reference", "table_reference"}):
            dest = ref.raw.strip()  # original casing, for a readable violation message
            break

        results = []
        for name in _shared.all_tables(stmt):
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
        """INSERT ... SELECT that reads PII into a table the manifest doesn't tag.

        Same underlying concern as _eval_select_into() above. Only reported when the DESTINATION is
        untagged - a copy from one tagged table into ANOTHER tagged table stays inside the
        classification and is deliberately not reported (see tests/pii/V002__clean.sql for that case).
        """
        dest = "another table"
        for child in stmt.segments or []:
            if child.get_type() == "table_reference":
                dest = child.raw.strip()
                break

        results = []
        seen = set()
        for name in _shared.source_tables(stmt):
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
            # execute_script_statement covers EXEC/EXECUTE - this is how sp_rename is reached, since
            # sp_rename is a stored procedure call rather than its own dedicated DDL statement type.
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
        # direct_tables(), not all_tables(): a table merely REFERENCED by an ALTER TABLE ... ADD
        # CONSTRAINT ... REFERENCES clause must not be reported here - only the table actually being
        # structurally changed matters for PI02. See _shared.direct_tables()'s docstring.
        for name in _shared.direct_tables(stmt):
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
        """T-SQL's sp_rename passes its target as a STRING LITERAL, not a table_reference.

        This is why sp_rename needs its own handling entirely separate from the direct_tables() path
        above - there is no table reference to resolve here at all. The literal's shape varies:
        'table', 'schema.table', 'db.schema.table', 'table.column' or 'schema.table.column'. Rather
        than guess which shape a given call uses, every plausible suffix combination is tested
        against both the table set and the column set. This coverage is T-SQL specific and does not
        port to other dialects' rename syntax (e.g. MySQL's RENAME TABLE) - a rule targeting another
        dialect needs its own equivalent branch here.
        """
        if "sp_rename" not in stmt.raw.lower():
            return None

        literals = _shared.descendants(stmt, {"quoted_literal"})
        if not literals:
            return None

        # Positionally the renamed object is the first argument, but sp_rename also accepts named
        # arguments (`EXEC sp_rename @newname = 'Client', @objname = 'dbo.Customer'`), where the
        # actual target can appear anywhere in the argument list.
        target_literal = literals[0]
        args = _shared.descendants(stmt, {"parameter", "quoted_literal"})
        for i, seg in enumerate(args):
            if seg.get_type() == "parameter" and _shared.normalize_identifier(seg.raw) == "@objname":
                named = [s for s in args[i + 1:]
                         if s.get_type() == "quoted_literal"]
                if named:
                    target_literal = named[0]
                break

        raw = target_literal.raw.strip("'\"")
        parts = [_shared.normalize_identifier(p) for p in raw.split(".") if p.strip()]
        if not parts:
            return None

        # Any trailing pair or single token could plausibly be "schema.table" or just "table" -
        # generate every candidate rather than assuming a fixed number of dot-separated parts.
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

        # None of the candidates matched a table - the last part might instead be a PII-tagged
        # COLUMN name being renamed on an otherwise-untagged table.
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
