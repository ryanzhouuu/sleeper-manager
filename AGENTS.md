# Repository Instructions

## Design documents

- Design documents, planning documents, brainstorming notes, and implementation plans may be created locally.
- Never stage or commit these documents to Git. Keep them out of Git history.
- Before creating a commit, verify that no design or planning documents are staged.
- Store local design and planning documents under `.local/design/`, which is ignored by Git.

## Verification

- Run formatting, linting, and tests before committing code changes.
- Never commit credentials, notification topics, access tokens, webhook URLs, or private API payloads.
