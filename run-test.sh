#!/bin/bash
cd "$(dirname "$0")"
docker run --rm --env-file .env -v "$(pwd)/.env:/app/.env" mazda-test
