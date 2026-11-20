-- Optional PostgreSQL initialization for the 知序 application.
-- The runtime installs the application schema, ordered migrations and bank mirror
-- when DATABASE_URL is configured. Keep schema definitions in the application.
CREATE EXTENSION IF NOT EXISTS pg_trgm WITH SCHEMA public;
