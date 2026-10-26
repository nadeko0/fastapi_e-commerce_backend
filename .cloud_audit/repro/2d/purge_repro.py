import time
from datetime import timedelta
from sqlalchemy import text, delete
from app.tasks import task_session, purge_expired_deletion_requests, engine
from app.models.user import User
from app.core.clock import utc_now
with engine.begin() as c:
    c.execute(text("DELETE FROM addresses")); c.execute(text("DELETE FROM users"))
with task_session() as db:
    old = utc_now() - timedelta(days=5)
    for i in range(100):
        db.add(User(email=f"u{i}@example.com", hashed_password="x", is_active=False,
                    data_deletion_requested=True, data_deletion_date=old))
    db.commit()
t = time.time()
try:
    with task_session() as db:
        n = purge_expired_deletion_requests(db)
    print("purged", n, "in", round(time.time()-t,1), "s")
except Exception as e:
    print("FAILED after", round(time.time()-t,1), "s:", type(e).__name__, str(e).splitlines()[0])
with engine.connect() as c:
    print("still pending:", c.execute(text("select count(*) from users where email like 'u%@example.com'")).scalar())
