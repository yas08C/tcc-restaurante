#!/usr/bin/env bash
# Script de build usado pelo Render.
set -o errexit

pip install -r requirements.txt

python manage.py collectstatic --noinput

python manage.py migrate
