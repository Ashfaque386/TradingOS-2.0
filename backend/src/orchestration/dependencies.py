"""Dependency resolver (Build Spec §7.3): tasks wait on typed artefact
dependencies, wake on completion, and are marked unsatisfiable on permanent
upstream failure.
"""

import uuid

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from src.models.result_artefact import ResultArtefact
from src.models.task import Task, TaskStatus
from src.models.task_dependency import TaskDependency


async def _promote_if_fully_satisfied(db: AsyncSession, task_id: uuid.UUID) -> None:
    result = await db.execute(
        select(TaskDependency.satisfied).where(TaskDependency.task_id == task_id)
    )
    flags = [row for (row,) in result.all()]
    if flags and all(flags):
        await db.execute(
            update(Task)
            .where(Task.id == task_id, Task.status == TaskStatus.PENDING)
            .values(status=TaskStatus.READY)
        )


async def resolve_dependencies_on_success(db: AsyncSession, completed_task_id: uuid.UUID) -> None:
    """Call after a task transitions to SUCCEEDED. Marks every
    TaskDependency waiting on it satisfied — respecting `artefact_type`
    when a dependency requires a specific one, not just upstream success —
    then promotes any now-fully-satisfied dependent from PENDING to READY.
    """
    deps_result = await db.execute(
        select(TaskDependency).where(TaskDependency.depends_on_task_id == completed_task_id)
    )
    dependencies = list(deps_result.scalars())
    if not dependencies:
        return

    produced_result = await db.execute(
        select(ResultArtefact.artefact_type).where(ResultArtefact.task_id == completed_task_id)
    )
    produced_types = {row for (row,) in produced_result.all()}

    affected_task_ids: set[uuid.UUID] = set()
    for dep in dependencies:
        if dep.satisfied:
            continue
        if dep.artefact_type is not None and dep.artefact_type not in produced_types:
            # Succeeded, but didn't produce the specific artefact type this
            # dependency requires -- not satisfied by this success alone.
            continue
        dep.satisfied = True
        affected_task_ids.add(dep.task_id)

    await db.flush()

    for task_id in affected_task_ids:
        await _promote_if_fully_satisfied(db, task_id)

    await db.commit()


async def propagate_unsatisfiable(db: AsyncSession, failed_task_id: uuid.UUID) -> None:
    """Call after a task's failure is classified PERMANENT (or after it
    itself becomes UNSATISFIABLE). Cascades UNSATISFIABLE to every direct
    and transitive dependent that hasn't already reached a terminal state.
    """
    frontier = [failed_task_id]
    seen: set[uuid.UUID] = set()

    while frontier:
        current = frontier.pop()
        if current in seen:
            continue
        seen.add(current)

        dependents_result = await db.execute(
            select(TaskDependency.task_id).where(TaskDependency.depends_on_task_id == current)
        )
        dependent_ids = {row for (row,) in dependents_result.all()}

        for dependent_id in dependent_ids:
            result = await db.execute(
                update(Task)
                .where(
                    Task.id == dependent_id,
                    Task.status.in_([TaskStatus.PENDING, TaskStatus.READY]),
                )
                .values(status=TaskStatus.UNSATISFIABLE)
            )
            if result.rowcount == 1:
                frontier.append(dependent_id)

    await db.commit()
