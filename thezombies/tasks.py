import json

from jsonschema import validate
import requests
from cachecontrol import CacheControl

from thezombies.factory import create_celery_app

celery = create_celery_app()

session = CacheControl(requests.Session(), cache_etags=False)

@celery.task
def fetch_url(url):
    resp = session.get(url)
    return resp

@celery.task
def parse_json(response):
    obj = None
    if response is None:
        raise Exception('Empty response')
    try:
        obj = response.json()
    except Exception as e:
        content_str = response.content.decode(response.apparent_encoding)
        try:
            obj = json.loads(content_str)
        except Exception as e:
            raise e
    return obj

@celery.task
def parse_json_from_job(job_id):
    j = Job.fetch(job_id)
    if j.is_finished:
        return parse_json(j.result)

@celery.task
def validate_json_from_job(job_id, schema_obj):
    j = Job.fetch(job_id)
    if j.is_finished:
        try:
            validate(j.result, schema_obj)
            return True
        except Exception as e:
            raise e

@celery.task
def find_access_urls(json_obj):
    pass

