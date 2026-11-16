from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.domain.models import Job, JobOutboxEvent, new_id


def record_job_dispatch(
    db: Session,
    job: Job,
    generation: int = 0,
    next_attempt_at: datetime | None = None,
) -> JobOutboxEvent:
    """Record queue intent in the same transaction that creates or retries its Job."""
    if job.id is None:
        # Assign early so the outbox row can reference the Job before the shared transaction commits.
        job.id = new_id()
    existing = db.query(JobOutboxEvent).filter(
        JobOutboxEvent.job_id == job.id,
        JobOutboxEvent.generation == generation,
    ).first()
    if existing:
        return existing
    event = JobOutboxEvent(
        job_id=job.id,
        generation=generation,
        status="pending",
        next_attempt_at=next_attempt_at or datetime.now(timezone.utc),
    )
    db.add(event)
    return event
