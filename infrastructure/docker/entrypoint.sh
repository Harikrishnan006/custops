#!/bin/sh
# Container entrypoint: bring the schema to head, then serve.
#
# Migrations run on start so that `docker compose up` on a clean volume produces
# a working system with no manual step (Phase 1 definition of done #8). This is
# a development-topology choice: a production deployment should run migrations
# as a separate, gated step rather than from every replica's start-up path, and
# that separation is Phase 14 work.
set -eu

echo "custops: applying database migrations"
alembic upgrade head

echo "custops: starting api"
exec uvicorn custops.apps.api.main:app --host 0.0.0.0 --port 8000
