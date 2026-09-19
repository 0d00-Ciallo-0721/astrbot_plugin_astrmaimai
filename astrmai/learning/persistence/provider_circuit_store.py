from __future__ import annotations

import time
import uuid
from dataclasses import dataclass
from pathlib import Path

from ...infrastructure.persistence.sqlite_helpers import connect_aiosqlite


_PROVIDER_FAILURES = frozenset({"provider_error", "provider_timeout"})


@dataclass(frozen=True, slots=True)
class LearningProviderCircuitState:
    provider_key: str
    task_family: str
    state: str
    failure_count: int
    window_started_at: float
    last_failure_at: float
    circuit_until: float
    half_open_owner: str
    half_open_token: str
    lease_until: float
    revision: int
    updated_at: float


@dataclass(frozen=True, slots=True)
class CircuitMutation:
    applied: bool
    conflict: bool = False
    idempotent: bool = False
    action: str = ""
    settlement_id: str = ""
    state: LearningProviderCircuitState | None = None
    resulting_state: str = ""
    resulting_revision: int = 0
    current_state: LearningProviderCircuitState | None = None

    def __post_init__(self) -> None:
        if self.current_state is None and self.state is not None:
            object.__setattr__(self, "current_state", self.state)
        if (self.applied or self.idempotent) and self.state is not None:
            if not self.resulting_state:
                object.__setattr__(self, "resulting_state", self.state.state)
            if self.resulting_revision <= 0:
                object.__setattr__(self, "resulting_revision", self.state.revision)


@dataclass(frozen=True, slots=True)
class CircuitDecision:
    allowed: bool
    reason: str
    provider_key: str
    task_family: str
    revision: int = 0
    circuit_until: float = 0.0
    lease_owner: str = ""
    lease_token: str = ""
    lease_until: float = 0.0
    retry_at: float = 0.0


class LearningProviderCircuitStore:
    FAILURE_THRESHOLD = 3
    FAILURE_WINDOW_SEC = 600.0
    COOLDOWN_SEC = 900.0

    _COLUMNS = (
        "provider_key, task_family, state, failure_count, window_started_at, "
        "last_failure_at, circuit_until, half_open_owner, half_open_token, "
        "lease_until, revision, updated_at"
    )

    def __init__(
        self,
        db_path: str | Path,
        *,
        failure_window_sec: float = FAILURE_WINDOW_SEC,
        cooldown_sec: float = COOLDOWN_SEC,
    ) -> None:
        self.db_path = Path(db_path)
        self.refresh(
            failure_window_sec=failure_window_sec,
            cooldown_sec=cooldown_sec,
        )

    def refresh(self, *, failure_window_sec: float, cooldown_sec: float) -> None:
        self.failure_window_sec = max(60.0, min(float(failure_window_sec), 3600.0))
        self.cooldown_sec = max(60.0, min(float(cooldown_sec), 86400.0))

    @staticmethod
    def resolve_provider_key(provider_id: str, provider_family: str) -> str:
        provider_id = str(provider_id or "").strip()
        if provider_id:
            return f"id:{provider_id}"
        provider_family = str(provider_family or "").strip().lower()
        return f"family:{provider_family}" if provider_family else ""

    @classmethod
    def _from_row(cls, row) -> LearningProviderCircuitState | None:
        if row is None:
            return None
        return LearningProviderCircuitState(
            provider_key=str(row[0]),
            task_family=str(row[1]),
            state=str(row[2]),
            failure_count=int(row[3]),
            window_started_at=float(row[4]),
            last_failure_at=float(row[5]),
            circuit_until=float(row[6]),
            half_open_owner=str(row[7]),
            half_open_token=str(row[8]),
            lease_until=float(row[9]),
            revision=int(row[10]),
            updated_at=float(row[11]),
        )

    async def _select(self, db, provider_key: str, task_family: str):
        cursor = await db.execute(
            f"SELECT {self._COLUMNS} FROM learning_provider_circuit "
            "WHERE provider_key = ? AND task_family = ?",
            (provider_key, task_family),
        )
        row = await cursor.fetchone()
        await cursor.close()
        return self._from_row(row)

    async def _select_settlement(self, db, settlement_id: str):
        cursor = await db.execute(
            """
            SELECT provider_key, task_family, action, failure_kind,
                   resulting_state, resulting_revision
            FROM learning_provider_circuit_settlement
            WHERE settlement_id = ?
            """,
            (settlement_id,),
        )
        row = await cursor.fetchone()
        await cursor.close()
        return row

    async def _existing_settlement_mutation(
        self,
        db,
        *,
        settlement_id: str,
        provider_key: str,
        task_family: str,
        action: str,
        failure_kind: str = "",
    ) -> CircuitMutation | None:
        existing = await self._select_settlement(db, settlement_id)
        if existing is None:
            return None
        state = await self._select(db, provider_key, task_family)
        matches = tuple(str(value or "") for value in existing[:4]) == (
            provider_key,
            task_family,
            action,
            failure_kind,
        )
        return CircuitMutation(
            applied=False,
            conflict=not matches,
            idempotent=matches,
            action=action,
            settlement_id=settlement_id,
            state=state,
            resulting_state=str(existing[4] or ""),
            resulting_revision=int(existing[5] or 0),
            current_state=state,
        )

    @staticmethod
    async def _insert_settlement(
        db,
        *,
        settlement_id: str,
        provider_key: str,
        task_family: str,
        action: str,
        failure_kind: str,
        state: LearningProviderCircuitState,
        created_at: float,
    ) -> None:
        await db.execute(
            """
            INSERT INTO learning_provider_circuit_settlement(
                settlement_id, provider_key, task_family, action, failure_kind,
                resulting_state, resulting_revision, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                settlement_id,
                provider_key,
                task_family,
                action,
                failure_kind,
                state.state,
                state.revision,
                created_at,
            ),
        )

    async def get_state(
        self,
        provider_key: str,
        task_family: str,
    ) -> LearningProviderCircuitState | None:
        if not provider_key or not task_family:
            return None
        async with connect_aiosqlite(self.db_path) as db:
            return await self._select(db, provider_key, task_family)

    async def record_failure(
        self,
        *,
        provider_key: str,
        task_family: str,
        failure_kind: str,
        settlement_id: str = "",
        expected_revision: int | None = None,
        lease_token: str = "",
        now: float | None = None,
    ) -> CircuitMutation:
        if not provider_key or not task_family or failure_kind not in _PROVIDER_FAILURES:
            return CircuitMutation(applied=False)
        observed_at = time.time() if now is None else float(now)
        settlement_id = str(
            settlement_id or f"legacy:failure:{uuid.uuid4().hex}"
        )
        async with connect_aiosqlite(self.db_path) as db:
            await db.execute("BEGIN IMMEDIATE")
            try:
                existing = await self._existing_settlement_mutation(
                    db,
                    settlement_id=settlement_id,
                    provider_key=provider_key,
                    task_family=task_family,
                    action="failure",
                    failure_kind=failure_kind,
                )
                if existing is not None:
                    await db.commit()
                    return existing
                current = await self._select(db, provider_key, task_family)
                if (
                    current is not None
                    and current.state == "half_open"
                    and (
                        expected_revision is None
                        or current.revision != expected_revision
                        or not current.half_open_token
                        or current.half_open_token != str(lease_token or "")
                    )
                ):
                    await db.rollback()
                    return CircuitMutation(
                        applied=False,
                        conflict=True,
                        action="failure",
                        settlement_id=settlement_id,
                        state=current,
                    )

                if current is None:
                    failure_count = 1
                    window_started_at = observed_at
                    revision = 1
                else:
                    window_expired = observed_at - current.window_started_at > self.failure_window_sec
                    failure_count = 1 if window_expired else current.failure_count + 1
                    window_started_at = observed_at if window_expired else current.window_started_at
                    revision = current.revision + 1
                should_open = (
                    failure_count >= self.FAILURE_THRESHOLD
                    or (current is not None and current.state in {"open", "half_open"})
                )
                state = "open" if should_open else "closed"
                circuit_until = observed_at + self.cooldown_sec if should_open else 0.0
                if current is None:
                    cursor = await db.execute(
                        """
                        INSERT INTO learning_provider_circuit(
                            provider_key, task_family, state, failure_count,
                            window_started_at, last_failure_at, circuit_until,
                            half_open_owner, half_open_token, lease_until,
                            revision, created_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, '', '', 0, ?, ?, ?)
                        """,
                        (
                            provider_key,
                            task_family,
                            state,
                            failure_count,
                            window_started_at,
                            observed_at,
                            circuit_until,
                            revision,
                            observed_at,
                            observed_at,
                        ),
                    )
                else:
                    cursor = await db.execute(
                        """
                        UPDATE learning_provider_circuit
                        SET state = ?, failure_count = ?, window_started_at = ?,
                            last_failure_at = ?, circuit_until = ?,
                            half_open_owner = '', half_open_token = '', lease_until = 0,
                            revision = ?, updated_at = ?
                        WHERE provider_key = ? AND task_family = ? AND revision = ?
                        """,
                        (
                            state,
                            failure_count,
                            window_started_at,
                            observed_at,
                            circuit_until,
                            revision,
                            observed_at,
                            provider_key,
                            task_family,
                            current.revision,
                        ),
                    )
                if cursor.rowcount != 1:
                    await db.rollback()
                    latest = await self.get_state(provider_key, task_family)
                    return CircuitMutation(
                        applied=False,
                        conflict=True,
                        action="failure",
                        settlement_id=settlement_id,
                        state=latest,
                    )
                updated = await self._select(db, provider_key, task_family)
                await self._insert_settlement(
                    db,
                    settlement_id=settlement_id,
                    provider_key=provider_key,
                    task_family=task_family,
                    action="failure",
                    failure_kind=failure_kind,
                    state=updated,
                    created_at=observed_at,
                )
                await db.commit()
                return CircuitMutation(
                    applied=True,
                    action="failure",
                    settlement_id=settlement_id,
                    state=updated,
                )
            except Exception:
                await db.rollback()
                raise

    async def check_or_claim(
        self,
        *,
        provider_key: str,
        task_family: str,
        owner: str,
        lease_seconds: float,
        now: float | None = None,
    ) -> CircuitDecision:
        if not provider_key:
            return CircuitDecision(False, "identity_unknown", "", task_family)
        observed_at = time.time() if now is None else float(now)
        async with connect_aiosqlite(self.db_path) as db:
            await db.execute("BEGIN IMMEDIATE")
            try:
                current = await self._select(db, provider_key, task_family)
                if current is None or current.state == "closed":
                    await db.commit()
                    return CircuitDecision(
                        True,
                        "closed",
                        provider_key,
                        task_family,
                        revision=current.revision if current else 0,
                    )
                if current.state == "open" and current.circuit_until > observed_at:
                    await db.commit()
                    return CircuitDecision(
                        False,
                        "circuit_open",
                        provider_key,
                        task_family,
                        revision=current.revision,
                        circuit_until=current.circuit_until,
                        retry_at=current.circuit_until,
                    )
                if current.state == "half_open" and current.lease_until > observed_at:
                    await db.commit()
                    return CircuitDecision(
                        False,
                        "half_open_busy",
                        provider_key,
                        task_family,
                        revision=current.revision,
                        circuit_until=current.circuit_until,
                        lease_owner=current.half_open_owner,
                        lease_token=current.half_open_token,
                        lease_until=current.lease_until,
                        retry_at=current.lease_until,
                    )

                token = uuid.uuid4().hex
                revision = current.revision + 1
                cursor = await db.execute(
                    """
                    UPDATE learning_provider_circuit
                    SET state = 'half_open', half_open_owner = ?, half_open_token = ?,
                        lease_until = ?, revision = ?, updated_at = ?
                    WHERE provider_key = ? AND task_family = ? AND revision = ?
                    """,
                    (
                        str(owner or ""),
                        token,
                        observed_at + max(0.1, float(lease_seconds or 0.1)),
                        revision,
                        observed_at,
                        provider_key,
                        task_family,
                        current.revision,
                    ),
                )
                if cursor.rowcount != 1:
                    await db.rollback()
                    return CircuitDecision(
                        False,
                        "half_open_busy",
                        provider_key,
                        task_family,
                        revision=current.revision,
                    )
                await db.commit()
                return CircuitDecision(
                    True,
                    "half_open_probe",
                    provider_key,
                    task_family,
                    revision=revision,
                    circuit_until=current.circuit_until,
                    lease_owner=str(owner or ""),
                    lease_token=token,
                    lease_until=observed_at + max(0.1, float(lease_seconds or 0.1)),
                )
            except Exception:
                await db.rollback()
                raise

    async def abort_half_open(
        self,
        *,
        provider_key: str,
        task_family: str,
        owner: str,
        lease_token: str,
        expected_revision: int,
        settlement_id: str = "",
        now: float | None = None,
    ) -> CircuitMutation:
        if not provider_key or not task_family or not owner or not lease_token:
            return CircuitMutation(applied=False)
        observed_at = time.time() if now is None else float(now)
        settlement_id = str(settlement_id or f"legacy:abort:{uuid.uuid4().hex}")
        async with connect_aiosqlite(self.db_path) as db:
            await db.execute("BEGIN IMMEDIATE")
            try:
                existing = await self._existing_settlement_mutation(
                    db,
                    settlement_id=settlement_id,
                    provider_key=provider_key,
                    task_family=task_family,
                    action="abort",
                )
                if existing is not None:
                    await db.commit()
                    return existing
                current = await self._select(db, provider_key, task_family)
                if (
                    current is None
                    or current.state != "half_open"
                    or current.revision != expected_revision
                    or current.half_open_owner != str(owner)
                    or current.half_open_token != str(lease_token)
                ):
                    await db.rollback()
                    return CircuitMutation(
                        applied=False,
                        conflict=True,
                        action="abort",
                        settlement_id=settlement_id,
                        state=current,
                    )
                cursor = await db.execute(
                    """
                    UPDATE learning_provider_circuit
                    SET state = 'open', circuit_until = ?,
                        half_open_owner = '', half_open_token = '', lease_until = 0,
                        revision = revision + 1, updated_at = ?
                    WHERE provider_key = ? AND task_family = ?
                      AND state = 'half_open' AND revision = ?
                      AND half_open_owner = ? AND half_open_token = ?
                    """,
                    (
                        observed_at,
                        observed_at,
                        provider_key,
                        task_family,
                        expected_revision,
                        str(owner),
                        str(lease_token),
                    ),
                )
                if cursor.rowcount != 1:
                    await db.rollback()
                    latest = await self.get_state(provider_key, task_family)
                    return CircuitMutation(
                        applied=False,
                        conflict=True,
                        action="abort",
                        settlement_id=settlement_id,
                        state=latest,
                    )
                updated = await self._select(db, provider_key, task_family)
                await self._insert_settlement(
                    db,
                    settlement_id=settlement_id,
                    provider_key=provider_key,
                    task_family=task_family,
                    action="abort",
                    failure_kind="",
                    state=updated,
                    created_at=observed_at,
                )
                await db.commit()
                return CircuitMutation(
                    applied=True,
                    action="abort",
                    settlement_id=settlement_id,
                    state=updated,
                )
            except Exception:
                await db.rollback()
                raise

    async def record_success(
        self,
        *,
        provider_key: str,
        task_family: str,
        expected_revision: int,
        lease_token: str = "",
        settlement_id: str = "",
        now: float | None = None,
    ) -> CircuitMutation:
        observed_at = time.time() if now is None else float(now)
        settlement_id = str(settlement_id or f"legacy:success:{uuid.uuid4().hex}")
        async with connect_aiosqlite(self.db_path) as db:
            await db.execute("BEGIN IMMEDIATE")
            try:
                existing = await self._existing_settlement_mutation(
                    db,
                    settlement_id=settlement_id,
                    provider_key=provider_key,
                    task_family=task_family,
                    action="success",
                )
                if existing is not None:
                    await db.commit()
                    return existing
                current = await self._select(db, provider_key, task_family)
                if current is None:
                    await db.rollback()
                    return CircuitMutation(
                        applied=False,
                        conflict=True,
                        action="success",
                        settlement_id=settlement_id,
                    )
                half_open_matches = bool(
                    current.state == "half_open"
                    and current.revision == expected_revision
                    and current.half_open_token
                    and current.half_open_token == str(lease_token or "")
                )
                closed_matches = bool(
                    current.state == "closed"
                    and current.revision == expected_revision
                )
                closed_already_clear = bool(
                    current.state == "closed"
                    and current.failure_count == 0
                    and current.window_started_at == 0
                )
                if not (half_open_matches or closed_matches or closed_already_clear):
                    await db.rollback()
                    return CircuitMutation(
                        applied=False,
                        conflict=True,
                        action="success",
                        settlement_id=settlement_id,
                        state=current,
                    )
                if closed_already_clear and not closed_matches:
                    await self._insert_settlement(
                        db,
                        settlement_id=settlement_id,
                        provider_key=provider_key,
                        task_family=task_family,
                        action="success",
                        failure_kind="",
                        state=current,
                        created_at=observed_at,
                    )
                    await db.commit()
                    return CircuitMutation(
                        applied=True,
                        action="success",
                        settlement_id=settlement_id,
                        state=current,
                    )
                cursor = await db.execute(
                    """
                    UPDATE learning_provider_circuit
                    SET state = 'closed', failure_count = 0, window_started_at = 0,
                        last_failure_at = 0, circuit_until = 0,
                        half_open_owner = '', half_open_token = '', lease_until = 0,
                        revision = revision + 1, updated_at = ?
                    WHERE provider_key = ? AND task_family = ? AND revision = ?
                    """,
                    (observed_at, provider_key, task_family, expected_revision),
                )
                if cursor.rowcount != 1:
                    await db.rollback()
                    return CircuitMutation(
                        applied=False,
                        conflict=True,
                        action="success",
                        settlement_id=settlement_id,
                        state=current,
                    )
                updated = await self._select(db, provider_key, task_family)
                await self._insert_settlement(
                    db,
                    settlement_id=settlement_id,
                    provider_key=provider_key,
                    task_family=task_family,
                    action="success",
                    failure_kind="",
                    state=updated,
                    created_at=observed_at,
                )
                await db.commit()
                return CircuitMutation(
                    applied=True,
                    action="success",
                    settlement_id=settlement_id,
                    state=updated,
                )
            except Exception:
                await db.rollback()
                raise


__all__ = [
    "CircuitDecision",
    "CircuitMutation",
    "LearningProviderCircuitState",
    "LearningProviderCircuitStore",
]
