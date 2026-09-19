import asyncio

import pytest

from astrmai.learning.runtime.learning_lane import (
    LearningLaneBudget,
    LearningLaneConfig,
    LearningWorkRequest,
)


TASK = "learning.expression_enrichment"


def _request(*, wait: float = 0.1, execution: float = 0.1) -> LearningWorkRequest:
    return LearningWorkRequest(
        run_id="run-1",
        candidate_id="candidate-1",
        scope_id="chat-1",
        task_name=TASK,
        workload_family="learning",
        wait_timeout_sec=wait,
        execution_timeout_sec=execution,
        config_revision=1,
    )


async def _wait_for_status(lane: LearningLaneBudget, key: str, value: int) -> None:
    for _ in range(100):
        if int(lane.status().get(key, 0)) == value:
            return
        await asyncio.sleep(0)
    raise AssertionError(f"lane status never reached {key}={value}: {lane.status()}")


@pytest.mark.asyncio
async def test_learning_lane_bounds_active_queue_and_reports_queue_full():
    lane = LearningLaneBudget(LearningLaneConfig(limit=1, max_queue=1))
    release = asyncio.Event()

    active = asyncio.create_task(
        lane.run_learning_work(_request(), lambda: release.wait())
    )
    await _wait_for_status(lane, "active", 1)
    queued = asyncio.create_task(
        lane.run_learning_work(_request(), lambda: asyncio.sleep(0))
    )
    await _wait_for_status(lane, "queued", 1)

    rejected = await lane.run_learning_work(
        _request(), lambda: asyncio.sleep(0)
    )
    assert rejected.status == "queue_full"
    assert rejected.failure_kind == "queue_full"
    assert rejected.acquired is False

    release.set()
    assert (await active).status == "completed"
    assert (await queued).status == "completed"
    assert lane.status()["active"] == 0
    assert lane.status()["queued"] == 0


@pytest.mark.asyncio
async def test_learning_lane_distinguishes_queue_and_execution_timeouts():
    lane = LearningLaneBudget(LearningLaneConfig(limit=1, max_queue=2))
    release = asyncio.Event()
    active = asyncio.create_task(
        lane.run_learning_work(_request(execution=1.0), lambda: release.wait())
    )
    await _wait_for_status(lane, "active", 1)

    queue_timeout = await lane.run_learning_work(
        _request(wait=0.01), lambda: asyncio.sleep(0)
    )
    assert queue_timeout.status == "queue_timeout"
    assert queue_timeout.acquired is False

    release.set()
    await active
    execution_timeout = await lane.run_learning_work(
        _request(execution=0.01), lambda: asyncio.sleep(10)
    )
    assert execution_timeout.status == "execution_timeout"
    assert execution_timeout.acquired is True
    assert execution_timeout.failure_kind == "execution_timeout"


@pytest.mark.asyncio
async def test_learning_lane_drain_rejects_new_claims_and_releases_leases():
    lane = LearningLaneBudget()
    lane.begin_drain()

    result = await lane.run_learning_work(
        _request(), lambda: asyncio.sleep(0)
    )

    assert result.status == "shutdown_rejected"
    assert result.failure_kind == "shutdown_rejected"
    assert result.acquired is False
    assert await lane.wait_until_idle(timeout_sec=0.1) == {"remaining": 0}


@pytest.mark.asyncio
async def test_learning_lane_propagates_owner_cancellation_and_releases_lease():
    lane = LearningLaneBudget()
    started = asyncio.Event()

    async def _work():
        started.set()
        await asyncio.Event().wait()

    task = asyncio.create_task(lane.run_learning_work(_request(), _work))
    await started.wait()
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task
    assert await lane.wait_until_idle(timeout_sec=0.1) == {"remaining": 0}


def test_learning_lane_rejects_unknown_task_name_and_clamps_limit():
    lane = LearningLaneBudget(LearningLaneConfig(limit=99, max_queue=8))
    assert lane.status()["limit"] == 2
    with pytest.raises(ValueError, match="unsupported learning task"):
        LearningWorkRequest(
            run_id="run-1",
            candidate_id=None,
            scope_id="chat-1",
            task_name="learning.unknown",
            workload_family="learning",
            wait_timeout_sec=1,
            execution_timeout_sec=1,
            config_revision=1,
        )


def test_evolution_manager_owns_one_lane_and_adapter_without_hidden_runtime_budget(tmp_path):
    from types import SimpleNamespace

    from config import AstrMaiConfig
    from astrmai.learning.evolution_manager import EvolutionManager

    config = AstrMaiConfig()
    assert config.evolution.learning_enrichment_queue_max == 8
    assert config.evolution.learning_enrichment_wait_timeout_sec == 10.0
    assert config.evolution.learning_enrichment_circuit_window_sec == 600
    assert config.evolution.learning_enrichment_cooldown_sec == 900
    gateway = SimpleNamespace(config=config)
    db = SimpleNamespace(
        memory_engine=None,
        persistence=SimpleNamespace(
            db_path=tmp_path / "owner.db",
            cache_dir=tmp_path,
        ),
    )
    manager = EvolutionManager(db, gateway, config=config, background_task_budget=None)

    assert manager.background_task_budget is None
    assert manager.expression_miner.enricher.background_task_budget is None
    assert manager.jargon_miner.enricher.background_task_budget is None
    assert manager.expression_miner.enricher.provider_adapter is manager.provider_adapter
    assert manager.jargon_miner.enricher.provider_adapter is manager.provider_adapter
    assert manager.expression_miner.enricher._legacy_provider_test_double is False
    assert manager.jargon_miner.enricher._legacy_provider_test_double is False
    assert manager.provider_adapter.learning_lane is manager.learning_lane

    lane_id = id(manager.learning_lane)
    adapter_id = id(manager.provider_adapter)
    manager.refresh_config(config)
    assert id(manager.learning_lane) == lane_id
    assert id(manager.provider_adapter) == adapter_id


@pytest.mark.asyncio
async def test_evolution_stop_drains_owned_learning_lane(tmp_path):
    from types import SimpleNamespace

    from config import AstrMaiConfig
    from astrmai.learning.evolution_manager import EvolutionManager

    config = AstrMaiConfig()
    manager = EvolutionManager(
        SimpleNamespace(
            memory_engine=None,
            persistence=SimpleNamespace(
                db_path=tmp_path / "stop.db",
                cache_dir=tmp_path,
            ),
        ),
        SimpleNamespace(config=config),
        config=config,
        background_task_budget=None,
    )

    await manager.stop_background_tasks()

    assert manager.learning_lane.status()["accepting"] is False
    result = await manager.learning_lane.run_learning_work(
        _request(), lambda: asyncio.sleep(0)
    )
    assert result.status == "shutdown_rejected"
