"""D1 reads for confirmation screens; owner edits are kept alongside their audit history."""

from sleeper_manager.persistence.d1 import D1StateRepository


class D1ManagerStore(D1StateRepository):
    """Reuse D1 decoding without expanding the scheduler repository's interface."""

    async def preview_token(self, token_hash: str) -> dict[str, object] | None:
        """Read a scoped token and its message without consuming it."""
        return await self._first(
            "SELECT t.action,t.expires_at,t.used_at,r.title,r.message,r.status "
            "FROM action_tokens t JOIN recommendations r USING (recommendation_id) "
            "WHERE t.token_hash=?",
            token_hash,
        )
