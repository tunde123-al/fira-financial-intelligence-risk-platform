-- Optional geospatial layer. Applied only when the PostGIS extension is available
-- (the docker-compose image `postgis/postgis` provides it).
CREATE EXTENSION IF NOT EXISTS postgis;

ALTER TABLE transactions ADD COLUMN IF NOT EXISTS geog geography(Point, 4326)
    GENERATED ALWAYS AS (
        CASE WHEN latitude IS NULL OR longitude IS NULL THEN NULL
             ELSE ST_SetSRID(ST_MakePoint(longitude, latitude), 4326)::geography END) STORED;
CREATE INDEX IF NOT EXISTS ix_tx_geog ON transactions USING gist (geog);

ALTER TABLE merchants ADD COLUMN IF NOT EXISTS geog geography(Point, 4326)
    GENERATED ALWAYS AS (
        CASE WHEN latitude IS NULL OR longitude IS NULL THEN NULL
             ELSE ST_SetSRID(ST_MakePoint(longitude, latitude), 4326)::geography END) STORED;
CREATE INDEX IF NOT EXISTS ix_merchants_geog ON merchants USING gist (geog);
