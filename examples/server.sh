#!/bin/sh

gunicorn -b "0.0.0.0:8082" -w 1 examples.server:app --worker-class aiohttp.GunicornWebWorker
