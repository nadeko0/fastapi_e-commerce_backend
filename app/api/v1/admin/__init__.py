from app.api.v1.admin._router import router

# isort: split
from app.api.v1.admin import products as products

# isort: split
from app.api.v1.admin import orders as orders

# isort: split
from app.api.v1.admin import variants as variants

# isort: split
from app.api.v1.admin import reconciliation as reconciliation

# isort: split
from app.api.v1.admin import webhook_events as webhook_events

# isort: split
from app.api.v1.admin import payments as payments

__all__ = ["router"]
