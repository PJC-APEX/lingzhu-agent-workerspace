from celery import Celery

from app.core.config import CELERY_BROKER_URL, CELERY_RESULT_BACKEND, JOB_OUTBOX_POLL_SECONDS

celery_app = Celery(
    "lingzhu",
    broker=CELERY_BROKER_URL,
    backend=CELERY_RESULT_BACKEND,
    include=["app.workers.local_tasks", "app.agents.memory_tasks"],
)
celery_app.conf.update(
    accept_content=["json"],
    task_serializer="json",
    result_serializer="json",
    result_expires=3600,
    broker_connection_retry_on_startup=True,
    task_track_started=True,
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    worker_prefetch_multiplier=1,
    broker_transport_options={"visibility_timeout": 7200},
    result_backend_transport_options={"visibility_timeout": 7200},
    timezone="UTC",
    enable_utc=True,
    beat_schedule={
        "dispatch-media-job-outbox": {
            "task": "lingzhu.dispatch_job_outbox",
            "schedule": JOB_OUTBOX_POLL_SECONDS,
        },
        "recover-media-jobs": {
            "task": "lingzhu.recover_media_jobs",
            "schedule": 30.0,
        },
    },
)
