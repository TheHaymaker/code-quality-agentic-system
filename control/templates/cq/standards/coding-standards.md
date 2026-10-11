# Coding standards (v0.1)

The standards reviewer checks new code against this page. Every rule can be
checked. A finding that cites a rule must name the rule and the line.

## 1. Core rules, all languages

1. **Delete more than you add.** Before you add a dependency or a helper, ask:
   does this need to exist, and does the codebase or the standard library
   already do it?
2. **No early abstraction.** Abstract when there are three real call sites,
   never on a guess.
3. **No dead code.** No speculative branches. No commented-out code.
4. **Errors are explicit.** No silent catch. If an error cannot happen, say why.
5. **A `shortcut:` comment names its limit.** Write
   `shortcut: <what is limited> until <condition>` so a sweep can find it.
6. **Comments say why, not what.**
7. **A function does one thing.** If you need "and" to describe it, split it.
8. **A name says the concept, not the type.** `remainingSlots`, not `integerVal`.
9. **Mutations are visible.** No hidden writes in getters or formatters.
10. **The diff is the unit of review.** These rules apply to new code.

## 2. Tests

1. A test asserts behaviour, not implementation detail.
2. Arrange, act, assert. One logical assertion per test.
3. The author of a test is not the author of the code under test.
4. Every bug fix has a regression test that fails without the fix.
5. A test is never skipped, deleted or weakened to make a run pass. A human
   approves any such change.

## 3. Prose: specs, tickets, commits, merge requests (ASD-STE100)

1. One idea per sentence. Short sentences. Common words.
2. No idioms. No metaphor. Active voice for instructions.
3. Commit subject: imperative, under 50 characters. The body says why.
4. A merge request states what changed, why, how it was verified, and the tier.
5. A ticket can be read by an agent that has no other context.

## 4. Language-specific standards

[FILL IN one section per language in this repository.]

### <Language>
- [FILL IN: the lint and format configuration that is authoritative]
- [FILL IN: idioms to use and constructs that are banned]

## 5. Change log

When the same class of finding appears three times, propose a rule here or a
lint rule. A change to this file is a tier 3 change and needs a human.

| Date | Rule | Change | Finding that caused it |
| --- | --- | --- | --- |
