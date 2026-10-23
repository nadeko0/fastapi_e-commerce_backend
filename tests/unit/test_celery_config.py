from app import tasks


def test_long_running_tasks_are_cancelled_on_connection_loss():
    """Tasks are acks_late, so a task running when the broker connection drops
    is redelivered. With the setting unset Celery 5.6 warns that the default
    becomes True in 6.0; setting it explicitly cancels the stale run instead of
    letting it execute in parallel with its redelivered copy."""
    conf = tasks.celery.conf
    assert conf.task_acks_late is True
    assert conf.worker_cancel_long_running_tasks_on_connection_loss is True
