# Repository Instructions

## Design documents

- Design documents, planning documents, brainstorming notes, and implementation plans may be created locally.
- Never stage or commit these documents to Git. Keep them out of Git history.
- Before creating a commit, verify that no design or planning documents are staged.
- Store local design and planning documents under `.local/design/`, which is ignored by Git.

## Verification

- Run formatting, linting, and tests before committing code changes.
- Never commit credentials, notification topics, access tokens, webhook URLs, or private API payloads.

## Git practices

- Commit frequently during implementation so each commit contains one small, coherent, reviewable unit of work and the history remains easy to follow.
- As a soft sizing target, aim for roughly 200 lines of source code and 200 lines of test code per commit when practical; coherence and a passing change take precedence over the count.
- Prefer several focused commits over one large commit that mixes unrelated behavior, refactoring, tests, or infrastructure changes.
- Do not create noisy checkpoint or broken WIP commits merely for frequency. Each commit should represent a meaningful completed step and pass the relevant formatting, linting, and tests.
- Use short, one-line commit messages with no body unless the user explicitly requests otherwise.
- Follow the concise `<type>: <imperative summary>` style, for example `chore: initialize sleeper manager` or `feat: sync league profile`.
- Do not mention local roadmap, design, implementation, or validation phases—or untracked planning documents—in commit messages; those are not durable repository context for reviewers.
- Make the subject describe the outcome of the commit, not the implementation process. Avoid vague messages such as `updates`, `changes`, or `fix stuff`.
- Before every commit, inspect the working tree, staged diff, and staged file list. Stage only the intended coherent change and preserve unrelated user work.
- Confirm that no design documents, planning files, secrets, private payloads, generated caches, or local configuration are staged.
- Keep behavior changes, mechanical refactors, and formatting-only changes in separate commits when separating them makes the history clearer.
- Do not amend, squash, rebase, force-push, or otherwise rewrite published history unless the user explicitly asks.

## Code hygiene

- Prefer clear names, small cohesive functions, and straightforward control flow over explanatory comments.
- Keep comments and docstrings as concise as possible. Use them only to explain non-obvious intent, constraints, invariants, tradeoffs, or external behavior that the code cannot express clearly.
- Explicitly prohibit "slop" comments: do not narrate obvious control flow, restate identifiers, paraphrase a function's implementation, repeat type information, or add decorative section commentary.
- Do not write comments that redundantly or trivially restate what the next line or block of code does.
- Keep necessary comments accurate as behavior changes. Remove stale comments, resolved TODOs, commented-out code, and obsolete compatibility notes.
- Make TODOs specific and actionable, with the missing decision or follow-up clearly identified; do not use vague placeholders.
- Avoid premature abstractions, dead code, duplicated logic, and unrelated cleanup. Keep changes focused on the requested behavior.
- Preserve explicit types and validate data at external boundaries rather than relying on comments to describe assumed shapes.

## Evidence and ambiguity

- Ground responses, plans, designs, and implementations in verifiable evidence such as repository state, tool output, tests, documented requirements, and authoritative primary sources.
- Inspect the relevant code, configuration, data, or documentation before making factual claims about the project. Do not guess or present an assumption as fact.
- Clearly distinguish verified facts from inferences, recommendations, and unresolved assumptions.
- When evidence is missing, stale, contradictory, or cannot be verified, state that limitation explicitly and identify what would be needed to resolve it.
- Ask the user before proceeding at any ambiguous design crossroads where the choice could materially affect architecture, behavior, scope, data semantics, compatibility, cost, or user experience.
- Ask the user when a required claim or decision cannot be grounded in available evidence. Do not invent details to keep moving.
- Routine implementation details with clear repository precedent may follow that precedent, but document any consequential assumption and verify it as soon as practical.

## Design alternatives and recommendations

- Whenever presenting a design question or consequential design decision, present two or three viable approaches grounded in the available evidence.
- Explain the material tradeoffs of each approach, including complexity, reliability, cost, maintainability, and user impact where relevant.
- Always identify a recommended approach and explain why it best fits the verified requirements and constraints.
- Do not manufacture superficial alternatives merely to satisfy the option count. If only one approach is genuinely viable, explain which alternatives were considered and why evidence rules them out.
- At an ambiguous design crossroads, present the approaches and recommendation, then ask the user to choose or approve before implementation proceeds.
