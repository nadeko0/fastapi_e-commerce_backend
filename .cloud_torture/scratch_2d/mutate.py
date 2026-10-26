"""Manual boundary-mutant runner: applies one operator flip at a time to a
COPY of the repo (/tmp/claude-mut-2d), runs test_boundary_mutants.py there and
records which tests kill it. Never touches the real app/."""
import json
import os
import re
import subprocess
import sys

COPY = "/tmp/claude-mut-2d"
PY = "/home/user/fastapi_e-commerce_backend/.venv/bin/python"
TEST = "tests/torture_suite/test_boundary_mutants.py"

MUTANTS = [
    ("M01", "app/core/rate_limit.py", "if count <= rate.count:", "if count < rate.count:"),
    ("M02", "app/core/rate_limit.py", "return max(1, math.ceil(ttl_ms / 1000))", "return max(1, math.floor(ttl_ms / 1000))"),
    ("M03", "app/core/rate_limit.py", "remaining = max(0, rate.count - count)", "remaining = max(0, rate.count - count + 1)"),
    ("M04", "app/services/redis_codec.py", "if stock_quantity < current_quantity + quantity:", "if stock_quantity <= current_quantity + quantity:"),
    ("M05", "app/services/redis_codec.py", "if stock_quantity < quantity:", "if stock_quantity <= quantity:"),
    ("M06", "app/services/redis_codec.py", "len(cart.items) >= settings.CART_MAX_LINES", "len(cart.items) > settings.CART_MAX_LINES"),
    ("M07", "app/schemas/common.py", "elif len(self.items) < max_lines:", "elif len(self.items) <= max_lines:"),
    ("M08", "app/services/redis_codec.py", "if line.quantity <= ordered_item.quantity:", "if line.quantity < ordered_item.quantity:"),
    ("M09", "app/api/v1/cart.py", "if available < item.quantity:", "if available <= item.quantity:"),
    ("M10", "app/api/v1/orders.py", "Product.stock_quantity >= item.quantity,", "Product.stock_quantity > item.quantity,"),
    ("M11", "app/tasks.py", "                Order.created_at < cutoff,\n            )\n        )\n    )", "                Order.created_at <= cutoff,\n            )\n        )\n    )"),
    ("M12", "app/tasks.py", "or order.created_at >= cutoff:", "or order.created_at > cutoff:"),
    ("M13", "app/tasks.py", "                Order.created_at < cutoff,\n            ).order_by", "                Order.created_at <= cutoff,\n            ).order_by"),
    ("M14", "app/tasks.py", "User.data_deletion_date <= grace_cutoff,", "User.data_deletion_date < grace_cutoff,"),
    ("M15", "app/tasks.py", "return now - last_seen > timedelta(days=days)", "return now - last_seen >= timedelta(days=days)"),
    ("M16", "app/services/reconciliation.py", "Payment.created_at >= now - timedelta(days=", "Payment.created_at > now - timedelta(days="),
    ("M17", "app/services/reconciliation.py", "Payment.created_at <= now - timedelta(minutes=", "Payment.created_at < now - timedelta(minutes="),
    ("M18", "app/services/reconciliation.py", "truncated=len(ids) > limit", "truncated=len(ids) >= limit"),
    ("M19", "app/services/reconciliation.py", "if consecutive_errors >= settings.RECONCILIATION_MAX_CONSECUTIVE_ERRORS:", "if consecutive_errors > settings.RECONCILIATION_MAX_CONSECUTIVE_ERRORS:"),
    ("M20", "app/api/v1/orders.py", "        Payment.created_at > started_after,\n    )\n    if except_payment_id", "        Payment.created_at >= started_after,\n    )\n    if except_payment_id"),
    ("M21", "app/services/stripe_events.py", "Payment.created_at > started_after,", "Payment.created_at >= started_after,"),
    ("M22", "app/services/stripe_events.py", "WebhookEvent.updated_at < now - timedelta(", "WebhookEvent.updated_at <= now - timedelta("),
    ("M23", "app/services/stripe_events.py", "if not 0 < order_id < 2 ** 31:", "if not 0 < order_id <= 2 ** 31:"),
    ("M24", "app/core/security.py", "return user_id if user_id <= _MAX_USER_ID else None", "return user_id if user_id < _MAX_USER_ID else None"),
    ("M25", "app/services/redis_async.py", "or issued_at < changed_at", "or issued_at <= changed_at"),
    ("M26", "app/core/security.py", "return issued_at, int(issued_at) + int(lifetime.total_seconds())", "return issued_at, int(issued_at) + int(lifetime.total_seconds()) + 1"),
    ("M27", "app/core/security.py", "expire = utc_now() + timedelta(minutes=settings.PASSWORD_RESET_TOKEN_EXPIRE_MINUTES)", "expire = utc_now() + timedelta(minutes=settings.PASSWORD_RESET_TOKEN_EXPIRE_MINUTES, seconds=1)"),
    ("M28", "app/core/security.py", "expire = utc_now() + timedelta(hours=settings.EMAIL_VERIFICATION_TOKEN_EXPIRE_HOURS)", "expire = utc_now() + timedelta(hours=settings.EMAIL_VERIFICATION_TOKEN_EXPIRE_HOURS, seconds=-1)"),
    ("M29", "app/api/v1/admin.py", "filters.append(Order.created_at >= as_utc(start_date))", "filters.append(Order.created_at > as_utc(start_date))"),
    ("M30", "app/api/v1/admin.py", "filters.append(Order.created_at <= as_utc(end_date))", "filters.append(Order.created_at < as_utc(end_date))"),
    ("M31", "app/api/v1/admin.py", "        Order.created_at >= start_date,", "        Order.created_at > start_date,"),
    ("M32", "app/services/gdpr.py", "return age_in_days <= retention_days", "return age_in_days < retention_days"),
    ("M33", "app/api/v1/products.py", "stmt = stmt.where(Product.price >= filter_params.min_price)", "stmt = stmt.where(Product.price > filter_params.min_price)"),
    ("M34", "app/api/v1/products.py", "stmt = stmt.where(Product.price <= filter_params.max_price)", "stmt = stmt.where(Product.price < filter_params.max_price)"),
    ("M35", "app/api/v1/products.py", "stmt = stmt.where(Product.stock_quantity > 0)", "stmt = stmt.where(Product.stock_quantity >= 0)"),
    ("M36", "app/services/payment/stripe_provider.py", "if abs(time.time() - timestamp) > tolerance_seconds:", "if abs(time.time() - timestamp) >= tolerance_seconds:"),
    ("M37", "app/api/v1/orders.py", "PaymentStatus.REFUNDED if returned >= payment_row.amount else PaymentStatus.PAID", "PaymentStatus.REFUNDED if returned > payment_row.amount else PaymentStatus.PAID"),
    ("M38", "app/services/stripe_events.py", "        settings.STRIPE_EVENT_RETRY_BASE_SECONDS * 2 ** (attempts - 1),", "        settings.STRIPE_EVENT_RETRY_BASE_SECONDS * 2 ** attempts,"),
    ("M39", "app/core/security.py", "expires_in = max(int(payload[\"exp\"] - time.time()), 1)", "expires_in = max(math.ceil(payload[\"exp\"] - time.time()), 1)"),
    ("M40", "app/core/rate_limit.py", "if ttl < 0 then\n", "if ttl < -1 then\n"),
    ("M41", "app/core/rate_limit.py", "return max(1, math.ceil(ttl_ms / 1000))", "return max(0, math.ceil(ttl_ms / 1000))"),
    ("M42", "app/api/v1/products.py", "has_more=total > filter_params.page * filter_params.size", "has_more=total >= filter_params.page * filter_params.size"),
    ("M43", "app/schemas/cart.py", "CartQuantity = Annotated[int, Field(ge=1, le=INT32_MAX)]", "CartQuantity = Annotated[int, Field(ge=0, le=INT32_MAX)]"),
    ("M44", "app/services/redis_codec.py", "return timedelta(days=settings.REDIS_GUEST_CART_TTL_DAYS)", "return timedelta(days=settings.REDIS_GUEST_CART_TTL_DAYS, milliseconds=2)"),
    ("M45", "app/api/v1/orders.py", "alias=\"Idempotency-Key\", min_length=8, max_length=255,\n        description=order_key_header_description(),", "alias=\"Idempotency-Key\", min_length=7, max_length=256,\n        description=order_key_header_description(),"),
    ("M46", "app/tasks.py", "cutoff = utc_now() - timedelta(minutes=settings.UNPAID_ORDER_EXPIRE_MINUTES)", "cutoff = utc_now() - timedelta(minutes=settings.UNPAID_ORDER_EXPIRE_MINUTES) + timedelta(microseconds=1)"),
]
# Mutants made of several edits applied together (equivalence check of the
# double guard in expire_unpaid_orders).
COMBINED = {"M11+M12": ["M11", "M12"], "M20+M21": ["M20", "M21"]}


def run(selection=None):
    cmd = [PY, "-m", "pytest", TEST, "-p", "no:cacheprovider", "-q", "--no-header",
           "-p", "no:logging", "-rf"]
    env = dict(os.environ)
    out = subprocess.run(cmd, cwd=COPY, capture_output=True, text=True, env=env).stdout
    failed = sorted(set(re.findall(r"^FAILED (\S+)", out, re.M)))
    summary = out.strip().splitlines()[-1] if out.strip() else "?"
    return failed, summary


def main():
    only = set(sys.argv[1:])
    baseline, summary = run()
    print("BASELINE", summary, file=sys.stderr)
    results = {}
    for mid, rel, old, new in MUTANTS:
        if only and mid not in only:
            continue
        path = os.path.join(COPY, rel)
        src = open(path).read()
        n = src.count(old)
        if n != 1:
            results[mid] = {"error": f"pattern occurs {n} times"}
            print(mid, "PATTERN", n, file=sys.stderr)
            continue
        if mid == "M39":
            new_src = src.replace(old, new).replace("import hashlib\n", "import hashlib\nimport math\n", 1)
        else:
            new_src = src.replace(old, new)
        open(path, "w").write(new_src)
        try:
            failed, summary = run()
        finally:
            open(path, "w").write(src)
        newly_failed = [t for t in failed if t not in baseline]
        fixed_bugs = [t for t in baseline if t not in failed]
        results[mid] = {"file": rel, "old": old.strip(), "new": new.strip(),
                        "killed_by": newly_failed, "bug_tests_now_passing": fixed_bugs,
                        "summary": summary}
        print(mid, "KILLED" if newly_failed else "SURVIVED", len(newly_failed),
              "fixes:", len(fixed_bugs), summary, file=sys.stderr)
    by_id = {m[0]: m for m in MUTANTS}
    for cid, parts in COMBINED.items():
        if only and cid not in only:
            continue
        saved = {}
        for pid in parts:
            _, rel, old, new = by_id[pid]
            path = os.path.join(COPY, rel)
            src = saved.get(path) or open(path).read()
            saved.setdefault(path, src)
            cur = open(path).read()
            open(path, "w").write(cur.replace(old, new))
        try:
            failed, summary = run()
        finally:
            for path, src in saved.items():
                open(path, "w").write(src)
        newly_failed = [t for t in failed if t not in baseline]
        fixed_bugs = [t for t in baseline if t not in failed]
        results[cid] = {"parts": parts, "killed_by": newly_failed,
                        "bug_tests_now_passing": fixed_bugs, "summary": summary}
        print(cid, "KILLED" if newly_failed else "SURVIVED", len(newly_failed),
              "fixes:", len(fixed_bugs), summary, file=sys.stderr)
    json.dump({"baseline": baseline, "results": results},
              open(os.path.join(os.path.dirname(__file__), "kill_matrix.json"), "w"), indent=1)


if __name__ == "__main__":
    main()
