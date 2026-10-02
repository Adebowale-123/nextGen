#!/usr/bin/env bash
# Render build: install, collect static files, migrate, and set up games, roles and the first admin.
set -o errexit

pip install --upgrade pip
pip install -r requirements.txt

python manage.py collectstatic --no-input
python manage.py migrate --no-input
python manage.py seed_games
python manage.py setup_roles
python manage.py ensure_admin
