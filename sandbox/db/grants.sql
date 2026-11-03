-- The API and the nightly report connect as role "acme" (peer auth over the Unix socket).
-- Tables are owned by postgres, so schema changes need the postgres superuser.
GRANT CONNECT ON DATABASE acme TO acme;
GRANT USAGE ON SCHEMA public TO acme;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO acme;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO acme;
