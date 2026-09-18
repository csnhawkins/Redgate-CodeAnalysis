# Requirements, reserved names, and rollout phasing

## Requirements

- **Flyway Enterprise only.** `check.sqlfluffCustomRulesPath` is an Enterprise-tier feature — this doesn't
  work on Community or Teams. Confirm the customer's (or your demo environment's) licence tier before
  promising this.
- **No local Python/SQLFluff install needed.** The Flyway CLI ships a packaged SQLFluff. Don't tell a
  customer they need to install Python — that used to be true in early drafts of internal docs and isn't
  anymore.
- The directory pointed at by `sqlfluffCustomRulesPath` must exist and contain `__init__.py`, or
  `check -code` aborts with a named error rather than silently running with rules missing — that's the
  behaviour you want, not a bug to work around.
- **GLIBC 2.35+ required on Linux build agents.** Below that, the packaged SQLFluff doesn't start at all:
  `version 'GLIBC_2.35' not found`. Documented distributions that meet it: Ubuntu 22.04+, Debian 12+,
  RHEL/Rocky/Alma 9.1+. Check this on every build agent before planning a rollout — check it **first** on
  agents serving on-prem or legacy rings, since those are most likely to be on an older base image. A gate
  that can't start on the agents serving the most conservative customers is a gate you don't have where you
  most need it.
- Confirm engine-version requirements for any specific feature being relied on (SARIF output, review
  profiles, dry-run deployment overview tables) — these have shipped at different engine versions and engine
  version is not tied to Flyway Desktop version, so don't infer one from the other.

## Reserved rule-code prefixes

- **`RG`** is reserved for Redgate's own rule library (`RG01`–`RG23` at last count, check the current
  library page for the latest number).
- **Stock SQLFluff prefixes are reserved too**: `AL`, `AM`, `CP`, `CV`, `JJ`, `LT`, `RF`, `ST`, `TQ`, and
  others. A custom rule using one of these loads with a warning on stderr and is dropped — the rest of the
  run continues, so this is easy to miss in a CI log until someone notices the rule never fires.
- Pick a genuinely free 2-letter prefix, and verify it doesn't collide with anything in either list before
  writing a line of code. See `plugin-architecture.md` for how to verify the class-naming regex separately
  — that's a different check from prefix reservation and both matter.
- If two mutually-exclusive naming-convention rules exist in the built-in library (as of writing, `RG18` and
  `RG19` are one such pair), enable only the one matching the actual naming convention in use — running both
  together is a guaranteed contradiction.

## Getting the output somewhere a reviewer actually reads it

Native SARIF 2.1.0 output means violations land as annotations directly on the pull request diff via GitHub
code scanning, rather than buried in a build log nobody opens unless the build goes red. Custom rule
violations are tagged `"ruleSource": "custom"` in the JSON/SARIF reports, which is useful if compliance
reporting needs to separate custom findings from the stock library. The `help` field is always `null` for
custom rules, since there's no Redgate documentation page to link to.

## Two settings that turn this from a suggestion into an actual control

- **`check.code.noqaSeverity = "ERROR"`.** A developer can suppress any rule inline with `-- noqa: XX01`,
  and by default that suppression only shows as a warning. Setting this to `ERROR` means a deliberate bypass
  of the gate still surfaces in the report and the build result. CLI-only setting.
- **Run `check -code` as its own pipeline step, not chained onto `migrate`.** Code review does not
  interrupt subsequent Flyway verb operations when they're chained — even with `failOnError` enabled, a
  chained `migrate` still runs after a failing review. This is the single most likely way to end up with a
  control that reports correctly and stops nothing.

## Rollout phasing

Do not switch a new gate to hard-fail on day one — that's how a rollout gets reversed after the first false
positive lands on someone's PR at a bad time.

1. **Audit.** `failOnError = false`. The rule reports, nothing fails. Run for a couple of sprints and read
   the output — this is where classification mistakes and false positives surface, before anyone's build is
   blocked by them.
2. **Soft gate.** Still non-blocking, but the report is reviewed on every pull request and a hit requires a
   comment explaining it. Change behaviour before enforcing it.
3. **Hard gate on the new rule(s) only**, at error severity. Everything else stays at whatever severity it
   was already at. A narrow gate that always holds beats a broad one that gets disabled the first time it's
   inconvenient.
4. **Widen** to the rest of the built-in library, or to additional custom rules, once the narrow gate has
   earned trust.

If a customer runs multiple product lines with different strictness needs, look at whether their Flyway
engine version supports named review profiles (`check.code.profile` / `check.code.profiles`) before
maintaining a separate `rules =` line per repository — profiles *exclude* rules or rule groups per profile,
which is the supported way to phase strictness per product line without forking the ruleset. Note what
profiles can't do: they can't hold specific rules at error severity while everything else sits at warning —
that's still severity configuration in the SQLFluff config file. The two mechanisms are complementary, not
interchangeable.

## What this kind of gate cannot do — say so up front

Set expectations with whoever's asking for this, before they find the gaps themselves:

- **Static analysis only sees the SQL text in the migration file.** Dynamic SQL built as a string, and a
  stored procedure call whose body does the risky thing internally, are invisible.
- **Views and synonyms aren't resolved.** Tagging a base table doesn't catch DML against a view built on
  top of it — the view itself needs tagging, or needs generating into the manifest from a dependency graph.
- **This governs deployment-time change, not runtime access.** It has nothing to say about who read or
  wrote sensitive data during normal application operation — that's database auditing/monitoring, a
  different conversation and different tooling. Decide which of the two is actually being asked for before
  building the wrong control carefully.
- **`check -code` reviews migration scripts from `filesystem:` locations.** A state-based project where the
  deployed SQL is generated rather than authored needs a different, later-stage gate — running the same
  rule logic against `check -dryrun` output instead, in the release loop rather than the author loop. That's
  a bigger, separate piece of work (it needs a connection to each target) and should only be built once the
  author-loop gate above is running and trusted.
