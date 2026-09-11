CREATE ROLE openmodel_owner LOGIN PASSWORD 'owner';
CREATE ROLE openmodel_app LOGIN PASSWORD 'app';
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
