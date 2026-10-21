#!/bin/sh
# Creates the role the application connects as (APP_DB_USER): it can log
# in and read and write rows, and nothing else - no superuser, no DDL, no
# new databases or roles. The postgres image runs this once, when it
# initialises an empty data directory, as POSTGRES_USER: the superuser the
# image creates, which stays the owner of the schema and is what the
# migrations run as (the migrate service in docker-compose.yml).
#
# An existing data volume was initialised without it. To add the role
# there, run it once by hand:
#   docker compose exec db sh /docker-entrypoint-initdb.d/10-app-role.sh
#
# The password is read by psql from its environment (\getenv, psql 15+)
# rather than put on a command line, where any process could read it.
set -eu

: "${APP_DB_USER:?APP_DB_USER must be set}"
: "${APP_DB_PASSWORD:?APP_DB_PASSWORD must be set}"
if [ "$APP_DB_USER" = "$POSTGRES_USER" ]; then
    echo "APP_DB_USER must differ from POSTGRES_USER (the superuser)" >&2
    exit 1
fi

psql -v ON_ERROR_STOP=1 --no-psqlrc --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<'SQL'
\getenv app_user APP_DB_USER
\getenv app_password APP_DB_PASSWORD
\getenv owner POSTGRES_USER
\getenv db POSTGRES_DB

CREATE ROLE :"app_user" LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS
    PASSWORD :'app_password';
GRANT CONNECT ON DATABASE :"db" TO :"app_user";
GRANT USAGE ON SCHEMA public TO :"app_user";

-- Whatever already exists (nothing, on a fresh volume)...
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO :"app_user";
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO :"app_user";
-- ...and every table and sequence the migrations create later, as the owner.
ALTER DEFAULT PRIVILEGES FOR ROLE :"owner" IN SCHEMA public
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO :"app_user";
ALTER DEFAULT PRIVILEGES FOR ROLE :"owner" IN SCHEMA public
    GRANT USAGE, SELECT ON SEQUENCES TO :"app_user";
SQL
