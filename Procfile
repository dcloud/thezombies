web: gunicorn thezombies.wsgi:application --log-file -
worker: celery -A thezombies worker --loglevel=info --concurrency=2 -P eventlet