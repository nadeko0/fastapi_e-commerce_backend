"""Scratch pytest plugin (not part of the suite): prototype of the proposed
fix for logout racing refresh - revoke_refresh_family writes a
`refresh_revoked:{family}` tombstone before reading the members, and
consume_refresh_token refuses a jti whose family carries one. app/ is not
edited."""
import json


def pytest_collection_finish(session):
    from app.services.redis_async import AsyncRedisService

    real_revoke = AsyncRedisService.revoke_refresh_family

    async def revoke_refresh_family(self, family_id):
        await self._redis.set(f"refresh_revoked:{family_id}", 1, ex=30 * 24 * 3600)
        return await real_revoke(self, family_id)

    async def consume_refresh_token(self, jti):
        key = f"refresh_active:{jti}"
        async with self._redis.pipeline() as pipe:
            pipe.get(key)
            pipe.delete(key)
            data, deleted = await pipe.execute()
        if not data or not deleted:
            return None
        payload = json.loads(data)
        if await self._redis.exists(f"refresh_revoked:{payload['family']}"):
            return None
        return payload

    AsyncRedisService.revoke_refresh_family = revoke_refresh_family
    AsyncRedisService.consume_refresh_token = consume_refresh_token
