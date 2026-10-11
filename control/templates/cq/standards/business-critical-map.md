# Business-critical map

This page says, in plain words, which parts of this system are expensive to get
wrong. Reviewers read it. You write it by hand.

This page does not enforce anything by itself. Enforcement is by path rules.
When you add a surface here, add its path glob to this repository's entry in
`registry.yml` in the control repo (or to `.cq/criticality.yml` to raise a tier).

## Tier 3, Critical

### Conversion funnel
[FILL IN: the exact user journey that produces revenue, as routes and services]
- Entry:
- Conversion action:
- Services and paths:
- Analytics events that measure it:

### Revenue and money movement
[FILL IN: payments, billing, pricing, subscriptions]

### Data integrity
[FILL IN: databases, migrations, backfills, pipelines, anything that can lose or corrupt data]

### Security and privacy
[FILL IN: authentication, authorization, personal data, secrets]

## Tier 2, High
[FILL IN: public API contracts, shared libraries, core business logic, infrastructure]

## Tier 1, Standard
Everything that no rule names. Ordinary features, internal tooling.

## Tier 0, Low
[FILL IN: documentation, internal scripts, experiments. Tier 0 paths are granted in the control repo only.]
