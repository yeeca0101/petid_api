from __future__ import annotations

import logging
import queue
import threading
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Generic, TypeVar

logger = logging.getLogger(__name__)

T = TypeVar("T")


class SchedulerFullError(RuntimeError):
    pass


@dataclass(frozen=True)
class LaneExecutionPolicy:
    lane_id: str
    device: str
    max_inflight_jobs: int
    local_queue_capacity: int
    enable_micro_batching: bool
    max_batch_items: int
    max_batch_wait_ms: int


@dataclass
class SchedulerTask(Generic[T]):
    job_id: uuid.UUID
    job_type: str
    payload: dict[str, Any]
    fn: Callable[[], T]
    done: threading.Event = field(default_factory=threading.Event)
    result: T | None = None
    error: BaseException | None = None


class SingleLaneScheduler:
    """V1 scheduler: one lane, bounded local queue, strict sequential execution, no batching."""

    def __init__(self, policy: LaneExecutionPolicy) -> None:
        if policy.max_inflight_jobs != 1:
            raise ValueError("V1 scheduler requires max_inflight_jobs=1")
        self.policy = policy
        self._queue: queue.Queue[SchedulerTask[Any]] = queue.Queue(maxsize=policy.local_queue_capacity)
        self._stop_event = threading.Event()
        self._thread = threading.Thread(target=self._run_loop, name=f"scheduler-{policy.lane_id}", daemon=True)
        self._started = False

    def start(self) -> None:
        if self._started:
            return
        if self.policy.enable_micro_batching:
            logger.warning(
                "Micro-batching requested but V1 scheduler does not support batching; running sequentially instead."
            )
        self._thread.start()
        self._started = True
        logger.info(
            "Single lane scheduler started | lane_id=%s | device=%s | local_queue_capacity=%s | batching=%s",
            self.policy.lane_id,
            self.policy.device,
            self.policy.local_queue_capacity,
            False,
        )

    def stop(self, timeout_s: float = 5.0) -> None:
        if not self._started:
            return
        self._stop_event.set()
        self._thread.join(timeout=timeout_s)

    def submit(self, task: SchedulerTask[T], *, block: bool = False, timeout: float | None = None) -> T:
        if not self._started:
            raise RuntimeError("Scheduler not started")
        try:
            self._queue.put(task, block=block, timeout=timeout)
        except queue.Full as exc:
            raise SchedulerFullError(
                f"Scheduler lane queue is full for lane_id={self.policy.lane_id}"
            ) from exc

        task.done.wait()
        if task.error is not None:
            raise task.error
        return task.result

    def queued_count(self) -> int:
        return self._queue.qsize()

    def has_capacity(self) -> bool:
        return not self._queue.full()

    def _run_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                task = self._queue.get(timeout=0.5)
            except queue.Empty:
                continue

            try:
                task.result = task.fn()
            except BaseException as exc:
                task.error = exc
            finally:
                task.done.set()
                self._queue.task_done()


def build_v1_scheduler(
    *,
    lane_id: str = "lane-0",
    device: str,
    local_queue_capacity: int,
    enable_micro_batching: bool,
    max_batch_items: int,
    max_batch_wait_ms: int,
) -> SingleLaneScheduler:
    return SingleLaneScheduler(
        LaneExecutionPolicy(
            lane_id=lane_id,
            device=device,
            max_inflight_jobs=1,
            local_queue_capacity=local_queue_capacity,
            enable_micro_batching=enable_micro_batching,
            max_batch_items=max_batch_items,
            max_batch_wait_ms=max_batch_wait_ms,
        )
    )
