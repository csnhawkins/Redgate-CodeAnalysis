---
name: redgate-flyway-sqlfluff-rule-builder
description: Guides building a custom SQLFluff rule that runs inside Flyway Enterprise's `check -code` command, to gate migrations on a compliance or governance concern the built-in Redgate rule library doesn't cover (e.g. "flag any change to tables holding personal data", "block schema changes to financial tables without sign-off", "require a ticket reference in every migration touching a specific schema", "enforce a naming convention only on one product line"). Trigger this whenever a Redgate colleague is writing or debugging a custom Flyway code review rule, asking how to plug a rule into SQLFluff for Flyway, wants to scope a built-in RGnn rule down to a specific set of objects, is troubleshooting why a custom rule isn't loading or firing, or is designing a PII/compliance gate for a customer demo or pilot. This captures hard-won architecture lessons (rule-code naming collisions, alias resolution, silent-failure modes) that are easy to get wrong from scratch — use it even if the request looks like "just write me a quick SQLFluff rule," since the pitfalls bite on the very first version.
---

# Flyway / SQLFluff custom rule builder

Flyway Enterprise's `check -code` command runs SQLFluff under the hood, and Redgate's rule library (`RG01`–`RG23` at last count) covers the generic stuff — unconditional DELETE, DROP DATABASE, data type changes. It does not, and cannot, cover **anything scoped to your specific objects or business rules** — "personal data columns," "the tables Finance cares about," "migrations without a JIRA reference." That's what a custom rule is for.

This skill exists because the first version of a custom rule almost always trips on the same handful of things, and they're expensive to discover late (a rule that silently never fires, or worse, silently stops enforcing after a version bump). Work through this rather than starting from a blank SQLFluff plugin tutorial.

A complete, working reference implementation (a PII-tagged-column gate, fully generic — no customer-specific content) lives at the end of this file's pointer in `references/worked-example.md`. Read it once to see the shape; don't just read this SKILL.md and start from zero.

## Step 1 — Pin down what "the rule" actually is

Before writing anything, get precise answers to:

1. **What triggers a violation?** Name the actual SQL shapes: which DML verbs, which DDL, against which objects. "PII" or "financial data" is a classification, not a rule — the rule is "DML that writes a column in that classification" plus "structural changes to a table in that classification."
2. **Does it matter whether the object is being read or written?** Almost always yes for a compliance rule — a `WHERE` clause reading a sensitive column is not the same risk as an `UPDATE` writing to it. Get this distinction explicit early; it drives the parse-tree logic in Step 3.
3. **What's the object list, and how often does it change?** This should almost never be hardcoded into the rule. See Step 2.
4. **Which dialect(s)?** T-SQL, PostgreSQL, MySQL, Oracle — statement segment names (`drop_table_statement`, `truncate_table`, etc.) are consistent across dialects, but dialect-specific constructs (T-SQL's `sp_rename`, the two-FROM `DELETE`) are not. Say up front which dialects need covering and which are nice-to-have.

## Step 2 — Separate the rule (code) from what it protects (data)

The single most important design decision: **the rule's Python logic should carry zero knowledge of which objects it protects.** Put the object/column list in a JSON manifest the rule loads at runtime, and let that file be the thing your team edits and reviews.

Why this matters in practice, not just in theory:
- Adding a newly-classified column becomes a one-line, no-code pull request — no re-testing the rule logic.
- The committed manifest is what a compliance reviewer can point to and say "this is what the build was gated against on this date." Querying a metadata service live at lint time loses that audit trail, and fails open silently if the query returns zero rows.
- The build agent running code review needs no database connection or credentials.

See `references/manifest-pattern.md` for the manifest shape, validation rules (fail loud on a malformed manifest — never let a bad shape silently narrow what's protected), and the environment-variable override pattern for pointing at a pipeline-generated manifest instead of a static file.

**Set up the folder shape now, even for a first rule.** Don't start with a flat `rules.py` — go straight to a `rules/_shared.py` (generic parse-tree/manifest helpers) plus `rules/<concern>.py` (the actual rule classes) split, with `manifests/`, `docs/` and `tests/<concern>/` folders alongside. Retrofitting this once a rule is already live in a pipeline means moving files around under a change a reviewer has to re-approve; starting there costs nothing extra on day one. See `references/folder-structure.md` for the full layout and why each split exists.

## Step 3 — Write the rule against SQLFluff's plugin API

Read `references/plugin-architecture.md` before writing a line of code — it covers the things that break the plugin if you don't know about them going in:

- **The rule-code naming trap.** SQLFluff extracts an exact 4-character code from the class name. A prefix that's 3 letters plus a 2-digit number (`Rule_PII01`, `Rule_PII02`) silently collides — both truncate to the same code and one rule vanishes with no error. Verify any prefix choice against the actual regex before committing to it; the reference file shows how.
- **No shared abstract base class.** SQLFluff validates every `BaseRule` subclass's name against its pattern, so an intermediate helper class that isn't itself a real rule breaks plugin loading. Put shared logic in module-level functions instead.
- **Rule imports go inside `get_rules()`**, not at module top level, or you get an import-order warning and, in older versions, a load failure.
- **Reserved prefixes**: `RG` is Redgate's. Stock SQLFluff prefixes (`AL`, `AM`, `CP`, `CV`, `JJ`, `LT`, `RF`, `ST`, `TQ`, and others) are reserved too — a rule using one loads silently-wrong (dropped with a stderr warning, easy to miss in CI logs).
- **Alias resolution.** If the rule cares about writes specifically, `UPDATE c SET c.SSN = '1' FROM dbo.Customer AS c` needs the alias `c` resolved back to `dbo.Customer` — matching on the literal token `c` catches nothing. `references/plugin-architecture.md` has the crawl pattern that handles this along with the MERGE/subquery/JOIN read-vs-write cases that are easy to get backwards.

## Step 4 — Build the proof pair, before you build the pipeline step

Write two test migrations, checked into the repo alongside the rule:

- One file where **every statement should violate**, covering every statement shape the rule claims to catch (including the awkward dialect-specific ones — T-SQL's `sp_rename` names its target in a string literal, not a table reference, so every literal shape needs its own test line).
- One file where **nothing should violate**, covering the near-misses that a naive text-matching or over-eager rule gets wrong: the sensitive column read only in a `WHERE` predicate, the sensitive table joined for reading only, the sensitive table as a MERGE `USING` source rather than target, a copy between two objects that are *both* in the protected classification.

This pair is not a formality — it's the control on the control. `references/ci-assertion-pattern.md` covers why: SQLFluff skips any file over a size ceiling and reports zero violations, identical to a clean pass. A rule that stops loading (renamed, dropped from the active `rules` line, a version bump that moved a plugin import) also produces zero violations and exit code 0. Asserting a specific nonzero count against the known-bad file in CI is the only thing that tells "the gate is clean" apart from "the gate silently died." Wire this into the pipeline as its own step before anything else.

No separate scratch Flyway project or migrations folder is needed to run these by hand while developing — target one script file directly: `flyway check -code -check.scope="script" -check.scriptFilename="code-review-rules/tests/<concern>/V001__..._violations.sql"`. See `references/folder-structure.md` for the exact command, why the resulting count needs filtering by rule code before asserting on it, and how to document the expected count per concern so it doesn't need reverse-engineering later.

## Step 5 — Wire it into Flyway and phase the rollout

- `flyway.toml`: `check.sqlfluffCustomRulesPath` points at the rule package directory; `check.rulesConfig` points at the SQLFluff config that lists the active rules (custom rules load but don't run unless they're in the `rules =` line).
- Run `check -code` as its **own pipeline step**. It does not interrupt a chained `migrate` — even with `failOnError` on, a chained deploy still runs after a failing review. If the gate needs to hold, review and deploy need to be separate steps whose exit codes the pipeline actually checks.
- Don't switch straight to hard-fail. Phase it: **audit** (report only, read the noise, fix tagging mistakes) → **soft gate** (still report-only, but every hit needs a reviewer comment) → **hard gate** on just the custom rule(s) at error severity → **widen** to the rest of the library once the narrow gate is trusted. A narrow gate that always holds beats a broad one that gets switched off after the first false positive.
- `references/requirements-and-gotchas.md` covers the environment landmines: Flyway Enterprise is required (this is not available on Community/Teams), the packaged SQLFluff needs GLIBC 2.35+ on Linux build agents (bites on-prem/legacy rings hardest), and which settings require which minimum engine version.

## Reference files

- `references/folder-structure.md` — the scalable folder layout to start with even for a single rule, why each split exists, the checklist for adding a new rule module, and how to run a proof pair without a scratch Flyway project
- `references/plugin-architecture.md` — the SQLFluff plugin mechanics: class-naming regex (with the verification method), crawler/segment types, alias resolution, read-vs-write distinction, the copy-detection pattern (`SELECT ... INTO` / `INSERT ... SELECT`)
- `references/manifest-pattern.md` — the object/column manifest shape, validation rules, the env-var override for a pipeline-generated manifest
- `references/ci-assertion-pattern.md` — the proof-pair testing pattern and the CI assertion that distinguishes a clean gate from a dead one
- `references/requirements-and-gotchas.md` — Flyway Enterprise requirement, GLIBC/engine version gotchas, reserved rule prefixes, rollout phasing detail
- `references/worked-example.md` — pointer to a complete, generic, working example (rule modules, manifests, docs, wiring config, and a verified test pair) to copy from rather than build from scratch
