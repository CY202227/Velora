"""HTTP client for atom-memory (contract.md — Atom-first paths)."""

from __future__ import annotations

import logging
import uuid
from typing import Any

import httpx

logger = logging.getLogger(__name__)


class AtomMemoryClient:
    def __init__(
        self,
        base_url: str,
        api_key: str = "",
        *,
        timeout: float = 30.0,
        consolidate_timeout: float = 120.0,
        delivery_store=None,
    ) -> None:
        self.delivery_store = delivery_store
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout = timeout
        self.consolidate_timeout = consolidate_timeout
        # uids we successfully ensured in this process (re-try after failure)
        self._ready: set[str] = set()

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["X-API-Key"] = self.api_key
        return headers

    async def ensure_space(self, uid: str, *, owner_id: str = "velora") -> dict[str, Any]:
        """Idempotent: create space if missing. Safe to call every turn."""
        if uid in self._ready:
            return {"uid": uid}
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.post(
                f"{self.base_url}/spaces",
                headers=self._headers(),
                json={"uid": uid, "owner_id": owner_id},
            )
            if resp.status_code in (200, 201):
                self._ready.add(uid)
                return resp.json()
            # Already exists / validation quirks — treat as ready if not auth failure
            if resp.status_code in (400, 409, 422):
                logger.info(
                    "space ensure: status=%s body=%s", resp.status_code, resp.text
                )
                self._ready.add(uid)
                return {"uid": uid}
            resp.raise_for_status()
            self._ready.add(uid)
            return resp.json()

    async def _ensure_or_bust(self, uid: str) -> bool:
        try:
            await self.ensure_space(uid)
            return True
        except Exception:
            self._ready.discard(uid)
            logger.exception("atom-memory ensure_space failed for %s", uid)
            return False

    async def recall(
        self,
        uid: str,
        query: str,
        *,
        method: str = "bm25",
        max_atoms: int = 5,
        budget_chars: int = 400,
        detail: str = "statement",
        policy: str = "layered",
        neighbor_hops: int = 0,
    ) -> dict[str, Any]:
        if not await self._ensure_or_bust(uid):
            return {"context_block": "", "hits": []}
        body = {
            "query": query,
            "method": method,
            "max_atoms": max_atoms,
            "budget_chars": budget_chars,
            "include_recent_sources": True,
            "detail": detail,
            "policy": policy,
            "neighbor_hops": neighbor_hops,
        }
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                resp = await client.post(
                    f"{self.base_url}/spaces/{uid}/recall",
                    headers=self._headers(),
                    json=body,
                )
                if resp.status_code == 404:
                    self._ready.discard(uid)
                    await self.ensure_space(uid)
                    resp = await client.post(
                        f"{self.base_url}/spaces/{uid}/recall",
                        headers=self._headers(),
                        json=body,
                    )
                resp.raise_for_status()
                return resp.json()
        except Exception:
            logger.exception("atom-memory recall failed")
            return {"context_block": "", "hits": []}

    async def add_source(
        self,
        uid: str,
        *,
        kind: str,
        content: str,
        salience: float = 0.2,
        external_ref: dict[str, Any] | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any] | None:
        payload: dict[str, Any] = {
            "kind": kind,
            "content": content,
            "salience": salience,
        }
        if idempotency_key is not None:
            payload["idempotency_key"] = idempotency_key
        if external_ref is not None:
            payload["external_ref"] = external_ref
        if self.delivery_store is not None:
            idempotency_key = idempotency_key or str(uuid.uuid4())
            payload["idempotency_key"] = idempotency_key
            await self.delivery_store.save_memory_delivery(idempotency_key, uid, payload)
        return await self._send_source(uid, payload)

    async def _send_source(self, uid: str, payload: dict) -> dict[str, Any] | None:
        if not await self._ensure_or_bust(uid):
            return None
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                resp = await client.post(
                    f"{self.base_url}/spaces/{uid}/sources",
                    headers=self._headers(),
                    json=payload,
                )
                if resp.status_code == 404:
                    self._ready.discard(uid)
                    await self.ensure_space(uid)
                    resp = await client.post(
                        f"{self.base_url}/spaces/{uid}/sources",
                        headers=self._headers(),
                        json=payload,
                    )
                resp.raise_for_status()
                return resp.json()
        except Exception:
            logger.exception("atom-memory add_source failed")
            return None

    async def consolidate(
        self, uid: str, *, trigger: str = "manual"
    ) -> dict[str, Any] | None:
        if not await self._ensure_or_bust(uid):
            return None
        try:
            async with httpx.AsyncClient(timeout=self.consolidate_timeout) as client:
                resp = await client.post(
                    f"{self.base_url}/spaces/{uid}/consolidate",
                    headers=self._headers(),
                    json={"trigger": trigger},
                )
                if resp.status_code == 404:
                    self._ready.discard(uid)
                    await self.ensure_space(uid)
                    resp = await client.post(
                        f"{self.base_url}/spaces/{uid}/consolidate",
                        headers=self._headers(),
                        json={"trigger": trigger},
                    )
                resp.raise_for_status()
                return resp.json()
        except Exception:
            logger.exception("atom-memory consolidate failed")
            return None

    async def synthesize(
        self, uid: str, *, trigger: str = "synthesize"
    ) -> dict[str, Any] | None:
        """L2 scenario synthesis. Independent of consolidate; call on a slower cadence."""
        return await self._post_space_job(
            uid, "synthesize", {"trigger": trigger}
        )

    async def persona(
        self, uid: str, *, trigger: str = "persona"
    ) -> dict[str, Any] | None:
        """L3 persona convergence. Independent of consolidate; call on a slower cadence."""
        return await self._post_space_job(
            uid, "persona", {"trigger": trigger}
        )

    async def _post_space_job(
        self,
        uid: str,
        path: str,
        payload: dict[str, Any],
    ) -> dict[str, Any] | None:
        if not await self._ensure_or_bust(uid):
            return None
        try:
            async with httpx.AsyncClient(timeout=self.consolidate_timeout) as client:
                resp = await client.post(
                    f"{self.base_url}/spaces/{uid}/{path}",
                    headers=self._headers(),
                    json=payload,
                )
                if resp.status_code == 404:
                    self._ready.discard(uid)
                    await self.ensure_space(uid)
                    resp = await client.post(
                        f"{self.base_url}/spaces/{uid}/{path}",
                        headers=self._headers(),
                        json=payload,
                    )
                resp.raise_for_status()
                return resp.json()
        except Exception:
            logger.exception("atom-memory %s failed", path)
            return None

    async def list_atoms(
        self,
        uid: str,
        *,
        page: int = 1,
        page_size: int = 50,
        kind: str | None = None,
    ) -> dict[str, Any]:
        await self.ensure_space(uid)
        params: dict[str, Any] = {"page": page, "page_size": page_size}
        if kind:
            params["kind"] = kind
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.get(
                f"{self.base_url}/spaces/{uid}/atoms",
                headers=self._headers(),
                params=params,
            )
            if resp.status_code == 404:
                self._ready.discard(uid)
                await self.ensure_space(uid)
                resp = await client.get(
                    f"{self.base_url}/spaces/{uid}/atoms",
                    headers=self._headers(),
                    params=params,
                )
            resp.raise_for_status()
            return resp.json()

    async def get_atom(
        self, uid: str, key: str, *, include: str = "revisions,evidence"
    ) -> dict[str, Any]:
        await self.ensure_space(uid)
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.get(
                f"{self.base_url}/spaces/{uid}/atoms/{key}",
                headers=self._headers(),
                params={"include": include} if include else None,
            )
            resp.raise_for_status()
            return resp.json()

    async def health(self) -> dict[str, Any]:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(
                f"{self.base_url}/health", headers=self._headers()
            )
            resp.raise_for_status()
            return resp.json()

    async def archive_atom(self, uid: str, key: str) -> dict[str, Any]:
        await self.ensure_space(uid)
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.post(
                f"{self.base_url}/spaces/{uid}/atoms/{key}/archive",
                headers=self._headers(),
            )
            resp.raise_for_status()
            return resp.json() if resp.content else {"ok": True}


    async def get_source(self, uid: str, source_id: int) -> dict[str, Any] | None:
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                # atom-memory exposes the source collection but not a
                # single-source endpoint. Filter locally so delivery retry
                # remains compatible with the HTTP contract.
                response = await client.get(
                    f"{self.base_url}/spaces/{uid}/sources",
                    headers=self._headers(),
                )
                response.raise_for_status()
                return next(
                    (
                        source
                        for source in response.json()
                        if source.get("id") == source_id
                    ),
                    None,
                )
        except Exception:
            logger.exception("atom-memory source status failed")
            return None

    async def find_source_by_external_ref(
        self, uid: str, external_ref: dict[str, Any]
    ) -> dict[str, Any] | None:
        """Find a previously delivered source without requiring a new API endpoint."""
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.get(
                    f"{self.base_url}/spaces/{uid}/sources",
                    headers=self._headers(),
                )
                response.raise_for_status()
                return next(
                    (
                        source
                        for source in response.json()
                        if source.get("external_ref") == external_ref
                    ),
                    None,
                )
        except Exception:
            logger.exception("atom-memory source lookup failed")
            return None


    async def retry_deliveries(self, consolidate_job=None) -> None:
        if self.delivery_store is None:
            return
        groups: dict[str, list[tuple[str, dict, dict]]] = {}
        for key, uid, payload in await self.delivery_store.pending_memory_deliveries():
            try:
                await self.delivery_store.touch_memory_delivery(key)
                external_ref = payload.get("external_ref")
                source = (
                    await self.find_source_by_external_ref(uid, external_ref)
                    if isinstance(external_ref, dict)
                    else None
                )
                source = source or await self._send_source(uid, payload)
                if source is None:
                    continue
                if source.get("status") in ("consolidated", "skipped"):
                    await self.delivery_store.finish_memory_delivery(key)
                    continue
                groups.setdefault(uid, []).append((key, payload, source))
            except Exception:
                logger.exception("memory delivery failed for %s", key)
        for uid, items in groups.items():
            try:
                trigger = "correction" if any(p["kind"] == "correction" for _, p, _ in items) else "scheduled"
                if consolidate_job is not None:
                    succeeded = await consolidate_job.run_now(uid, trigger=trigger)
                else:
                    result = await self.consolidate(uid, trigger=trigger)
                    succeeded = bool(result and result.get("status") == "succeeded")
                if not succeeded:
                    continue
                for key, _, source in items:
                    current = await self.get_source(uid, source["id"])
                    if current and current.get("status") in ("consolidated", "skipped"):
                        await self.delivery_store.finish_memory_delivery(key)
            except Exception:
                logger.exception("memory delivery consolidation failed for %s", uid)
