#!/bin/bash
# Runs once, from docker-entrypoint-initdb.d, only when the data dir is empty.
# Shared by compose (defaults below) and the cluster (OWNER_PASSWORD/APP_PASSWORD
# from the postgres-credentials Secret). The whole heredoc goes to one psql so
# the \c meta-command switches connection mid-script.
set -euo pipefail

: "${OWNER_PASSWORD:=owner}"
: "${APP_PASSWORD:=app}"
: "${MONITOR_PASSWORD:=monitor}"

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname openmodel <<-EOSQL
	CREATE ROLE openmodel_owner LOGIN PASSWORD '${OWNER_PASSWORD}';
	CREATE ROLE openmodel_app LOGIN PASSWORD '${APP_PASSWORD}';
	-- Read-only metrics role for the postgres-exporter sidecar. pg_monitor is a
	-- built-in role: pg_stat_* views (incl. pg_stat_statements) without any DML.
	CREATE ROLE openmodel_monitor LOGIN PASSWORD '${MONITOR_PASSWORD}';
	GRANT pg_monitor TO openmodel_monitor;
	GRANT CONNECT ON DATABASE openmodel TO openmodel_monitor;
	ALTER DATABASE openmodel OWNER TO openmodel_owner;
	GRANT CONNECT ON DATABASE openmodel TO openmodel_app;
	\c openmodel
	ALTER SCHEMA public OWNER TO openmodel_owner;
	GRANT USAGE ON SCHEMA public TO openmodel_app;
	ALTER DEFAULT PRIVILEGES FOR ROLE openmodel_owner IN SCHEMA public
	  GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO openmodel_app;
	ALTER DEFAULT PRIVILEGES FOR ROLE openmodel_owner IN SCHEMA public
	  GRANT USAGE, SELECT ON SEQUENCES TO openmodel_app;
	CREATE EXTENSION IF NOT EXISTS pg_stat_statements;
	ALTER ROLE openmodel_app SET statement_timeout = '10s';
	ALTER ROLE openmodel_app SET idle_in_transaction_session_timeout = '30s';
	ALTER ROLE openmodel_app SET lock_timeout = '2s';
	ALTER ROLE openmodel_owner SET lock_timeout = '5s';
EOSQL
