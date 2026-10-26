import os, sys, subprocess, pprint
from sqlalchemy import create_engine, text, inspect
from sqlalchemy.schema import CheckConstraint
ADMIN = "postgresql+psycopg://postgres@127.0.0.1:55432/postgres"
URL = "postgresql+psycopg://postgres@127.0.0.1:55432/drift2d"
a = create_engine(ADMIN, isolation_level="AUTOCOMMIT")
with a.connect() as c:
    c.execute(text('DROP DATABASE IF EXISTS drift2d WITH (FORCE)')); c.execute(text('CREATE DATABASE drift2d'))
env = dict(os.environ, DATABASE_URI=URL)
r = subprocess.run([sys.executable, "-m", "alembic", "upgrade", "head"], env=env, capture_output=True, text=True)
print("alembic rc", r.returncode, r.stderr[-400:])
os.environ["DATABASE_URI"] = URL
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
import app.models
from app.models.base import Base
e = create_engine(URL)
with e.connect() as conn:
    ctx = MigrationContext.configure(conn, opts={"compare_type": True, "compare_server_default": True})
    diff = compare_metadata(ctx, Base.metadata)
    print("compare_metadata diff:", diff)
    insp = inspect(conn)
    # CHECK constraints (not compared by autogenerate)
    for t in Base.metadata.sorted_tables:
        model_ck = {c.name for c in t.constraints if isinstance(c, CheckConstraint)}
        db_ck = {c["name"] for c in insp.get_check_constraints(t.name)}
        if model_ck != db_ck:
            print("CHECK mismatch", t.name, "model-only", model_ck - db_ck, "db-only", db_ck - model_ck)
        # FK ondelete
        mfk = {(tuple(sorted(c.name for c in fk.columns)), fk.ondelete) for fk in t.foreign_key_constraints}
        dfk = {(tuple(sorted(fk["constrained_columns"])), (fk.get("options") or {}).get("ondelete")) for fk in insp.get_foreign_keys(t.name)}
        if {(c, (o or "").upper() or None) for c,o in mfk} != {(c, (o or "").upper() or None) for c,o in dfk}:
            print("FK mismatch", t.name, mfk, dfk)
        # constraint/index names
        mnames = {i.name for i in t.indexes} | {c.name for c in t.constraints if c.name}
        dnames = {i["name"] for i in insp.get_indexes(t.name)} | {u["name"] for u in insp.get_unique_constraints(t.name)} | {c["name"] for c in insp.get_check_constraints(t.name)} | {f["name"] for f in insp.get_foreign_keys(t.name)} | {insp.get_pk_constraint(t.name)["name"]}
        if mnames - dnames or dnames - mnames:
            print("NAME mismatch", t.name, "model-only", mnames - dnames, "db-only", dnames - mnames)
    print("tables:", len(Base.metadata.tables), sorted(insp.get_table_names()))
