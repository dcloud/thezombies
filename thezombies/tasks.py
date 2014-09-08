from __future__ import absolute_import
from itertools import islice

try:
    import simplejson as json
except ImportError:
    import json
import ujson

from jsonschema import Draft4Validator, ValidationError
import requests
from cachecontrol import CacheControl
from celery import shared_task, chord, chain, group
from celery.result import AsyncResult
from celery.utils.log import get_task_logger

from django.conf import settings
from django.db import transaction, DatabaseError
from django.core.exceptions import ObjectDoesNotExist
import requests
from requests.exceptions import (MissingSchema, InvalidSchema, InvalidURL)

from thezombies.models import (Report, URLInspection, Agency)

session = CacheControl(requests.Session(), cache_etags=False)
logger = get_task_logger(__name__)

SCHEMA_ERROR_LIMIT = 100
SCHEMA_PATH = getattr(settings, 'DATA_CATALOG_SCHEMA_PATH', None)
catalog_schema = json.load(open(SCHEMA_PATH, 'r')) if SCHEMA_PATH else None
validator = Draft4Validator(catalog_schema)

class ResultDict(dict):
    """
        Provides a dict-like object with an errors list.
        Vulnerable to overwriting errors using .update(), so don't do that.
    """
    def __init__(self, data=None, errors=None):
        super(ResultDict, self).__init__()
        self._errors = errors if errors else []
        if data:
            self.update(data)
            if not errors and hasattr(data, 'errors'):
                self._errors.extend(data.errors)
        self['errors'] = self._errors

    def add_error(self, error):
        """Provide an error object, ResultDict will store the class and value of that error"""
        if error:
            error_name = error.__class__.__name__
            if error.message and error.message != '':
                error_message = error.message
            else:
                 error_message = ', '.join([str(a) for a in error.args])
            if isinstance(error, ValidationError):
                error_message = '{} >>\n {}'.format(error.message, error.schema)
            error_str = '{0}: {1}'.format(error_name, error_message)
            self._errors.append(error_str)
            self['errors'] = self._errors

    @property
    def errors(self):
        return self._errors

@shared_task
def error_handler(uuid):
    result = AsyncResult(uuid)
    exc = result.get(propagate=False)
    logger.warn('Task {0} raised exception: {1!r}\n{2!r}'.format(uuid, exc, result.traceback))

@shared_task
def check_and_correct_url(url, method='GET'):
    """Check a url for issues, record exceptions, and attempt to correct the url.

    :param url: URL to check and correct
    :param method: http method to use, as a string. Default is 'GET'
    """
    returnval = ResultDict({'initial_url': url})
    req = requests.Request(method.upper(), url)
    try:
        preq = req.prepare()
    except MissingSchema as e:
        returnval.add_error(e)
        new_url = 'http://{}'.format(req.url)
        req.url = new_url
        try:
            preq = req.prepare()
            returnval['corrected_url'] = preq.url
        except Exception as e:
            returnval.add_error(e)
    except Exception as e:
        returnval.add_error(e)

    return returnval

@shared_task
def request_url(url, method='GET'):
    """Task to request a url, a GET request by default. Tracks and returns errors.

    :param url: URL to request
    :param method: http method to use, as a string. Default is 'GET'
    """
    resp = None
    checker_result = check_and_correct_url(url)
    valid_url = checker_result.get('corrected_url', url)
    returnval = ResultDict(checker_result)
    try:
        resp = session.request(method.upper(), valid_url, allow_redirects=True)
    except Exception as e:
        returnval.add_error(e)
    # a non-None requests.Response will evaluate to False if it carries an HTTPError value
    if resp is not None:
        try:
            resp.raise_for_status()
        except Exception as e:
            returnval.add_error(e)
    returnval['response'] = resp
    return returnval

@shared_task
def get_or_create_inspection(url):
    """Task to get the lastest URLInspection or create a new one if none exists.

    :param url: The url to retrieve.
    """
    latest_dates = URLInspection.objects.datetimes('created_at', 'minute')
    recent_responses = None
    if latest_dates:
        latest_date = latest_dates.latest()
        recent_responses = URLInspection.objects.filter(requested_url=url, created_at__day=latest_date.day, parent_id__isnull=True)

    response = None
    if recent_responses and recent_responses.count() > 0:
        response = recent_responses.latest()
    else:
        logger.info('No stored response, fetch url')
        fetch_val = request_url(url)
        resp_data = fetch_val.get('response', None)
        with transaction.atomic():
            response = URLInspection.objects.create_from_response(resp_data)
            response.save()
    return ResultDict({'response_id': getattr(response, 'id', None), 'url':url})

@shared_task
def create_data_crawl_report(agency_id, catalog_url):
    """Create a report to track the crawl of a data catalog url and
    spawns tasks to inspect individual objects in the catalog

    :param agency_id: Database id of the agency whose catalog should be searched
    :param catalog_url: The url of the catalog to search. Generally accessible on agency.data_json_url
    """
    fetcher = get_or_create_inspection(catalog_url)
    response_id = fetcher.get('response_id')
    response = URLInspection.objects.get(id=response_id)

    parse_args = {'content':response.content.string()}
    parse_args['encoding'] = response.encoding if response.encoding else response.apparent_encoding
    result_dict = parse_json(parse_args)
    jsondata = result_dict.get('json', None)
    returnval = ResultDict({'agency_id': agency_id, 'catalog_url':catalog_url})
    report_id = None
    with transaction.atomic():
        report = Report.objects.create(agency_id=agency_id, report_type=Report.DATA_CATALOG_CRAWL, url=catalog_url)
        report_id = report.id
        if jsondata:
            catalog_length = len(jsondata)
            report.messages.append("Data catalog contains {0} items".format(catalog_length))
            report.save()
    returnval['report_id'] = report_id
    if jsondata:
        for item in jsondata:
            taskarg = {'agency_id': agency_id, 'report_id': report_id, 'catalog_url':catalog_url, 'item': item}
            inspect_data_catalog_item.delay(taskarg)
    else:
        # Report some error or something
        with transaction.atomic():
            report.messages.append("Unable to load json data from '{0}'. Cannot find urls for datasets")
    return returnval

@shared_task
def inspect_data_catalog_item(taskarg):
    """Inspect an item (json object) in a data catalog (json array) and check any included accessURLs, distributions or webServices

    :param taskarg: Dictionary containing the json object to inspect, a report id, agency_id and catalog_url
    """
    # I don't think tasks work properly in an inner function...
    def make_task(field, item, orig_task):
        url = item.get(field, None)
        if url:
            task_dict = ResultDict(orig_task)
            task_dict['url'] = url
            task_dict['urlType'] = field
            return task_dict
        return None

    def find_tasks(item, url_fields, taskarg):
        tasks = []
        for field in url_fields:
            if field in item:
                    task_dict = make_task(field, item, taskarg)
                    if task_dict:
                        tasks.append(task_dict)
                    else:
                        logger.error('Unable to make a task dictionary to pass to inspect_data_catalog_item_url')
            else:
                logger.warn("No '{0}' in item.".format(field))
        return tasks

    item = taskarg.get('item', None)
    report_id = taskarg.get('report_id', None)
    meta_fields = ('title', 'accessLevel', 'publisher', 'modified')
    taskarg['item_info'] = { key: item.get(key, None) for key in meta_fields }
    item_title = item.get('title', 'No title provided.')
    task_args = []
    if item:
        url_fields = ('accessURL', 'webService')
        task_args.extend(find_tasks(item, url_fields, taskarg))
        if 'distribution' in item:
            distribution = item.get('distribution', None)
            if distribution:
                for d in distribution:
                    task_args.extend(find_tasks(item, url_fields, taskarg))

        if len(task_args) > 0:
            for t in task_args:
                inspect_data_catalog_item_url.delay(t)
        else:
            if report_id:
                with transaction.atomic():
                    report = Report.objects.get(id=report_id)
                    report.messages.append("No urls found for catalog item titled '{0}'".format(item_title))
                    report.save()


    else:
        logger.warn('No item passed to inspect_data_catalog_item')


@shared_task(ignore_result=True, rate_limit='10/s')
def inspect_data_catalog_item_url(taskarg):
    """Task to check an accessURL from a data catalog, using a HEAD request. Tracks and returns errors.

    :param taskarg: A dictionary containing a url, and optionally a report_id
    """
    returnval = ResultDict(taskarg)
    url = taskarg.get('url', None)
    urlType = taskarg.get('urlType', None)
    item_info = taskarg.get('item_info', {})
    if urlType:
        item_info['urlType'] = urlType
    logger.info(item_info)
    report_id = taskarg.get('report_id', None)
    if url:
        result = request_url(url, 'HEAD')
        response = result.get('response', None)
        returnval.errors.extend(result.errors)
        if response is not None:
            with transaction.atomic():
                resp_obj = URLInspection.objects.create_from_response(response, save_content=False)
                resp_obj.info.update(item_info)
                resp_obj.errors = result.errors
                if report_id:
                    resp_obj.report_id = report_id
                resp_obj.save()
                returnval['response_id'] = resp_obj.id
        else:
            with transaction.atomic():
                resp_obj = URLInspection.objects.create(requested_url=url, errors=result.errors, info=item_info)
                if report_id:
                    resp_obj.report_id = report_id
                resp_obj.save()
                returnval['response_id'] = resp_obj.id

    return returnval

@shared_task
def crawl_agency_datasets(agency_id):
    """Task that crawl the datasets from an agency data catalog.
    Runs create_data_crawl_report, which spawns inspect_data_catalog_item tasks which in turn spawns
    inspect_data_catalog_item_url tasks.

    :param agency_id: Database id of the agency whose catalog should be crawled.

    """
    agency = Agency.objects.get(id=agency_id)
    return create_data_crawl_report.apply_async((agency.id, agency.data_json_url),
                              options={'link_error':error_handler.s()})

@shared_task
def report_for_agency_url(agency_id, url, report_type=Report.GENERIC_REPORT):
    """Task to save a basic report given an agency_id and a url.

    :param agency_id: Database id of the agency to create a report for.
    :param url: URL to report on.
    :param report_type: Optional report type (as provided by Report model)

    """
    result = request_url((url))
    returnval = ResultDict(result)
    resp_data = result.get('response', None)
    report_id = response_id = None
    response_info = {}
    response = None
    with transaction.atomic():
        if resp_data is not None:
            response = URLInspection.objects.create_from_response(resp_data)
            report = Report.objects.create(agency_id=agency_id, url=response.requested_url)
        else:
            report = Report.objects.create(agency_id=agency_id)
        report.report_type = report_type
        report.save()
        returnval['report_id'] = report.id
        if response:
            response.errors.extend(returnval.errors)
            report.inspections.add(response)
            response.save()
            returnval['response_id'] = response.id
    if resp_data and not resp_data.ok:
        # If the response is not okay, raise an error so we can handle that as an error
        resp_data.raise_for_status()
    returnval['response_info'] = response_info
    return returnval

@shared_task
def parse_json(taskarg):
    """
    Task to parse json from content

    :param taskarg: ResultDict or regular dict containing values for keys 'content and optionally 'encoding'.
    """
    if isinstance(taskarg, tuple):
        taskarg = taskarg[0]
    content = taskarg.get('content', None)
    encoding = taskarg.get('encoding', 'iso-8859-1')
    jsondata = None
    parse_errors = False
    returnval = ResultDict()
    if content is None:
        returnval.add_error(Exception('No content to parse'))
    else:
        try:
            jsondata = json.loads(content, encoding=encoding)
        except Exception as e:
            parse_errors = True
            returnval.add_error(e)
            content_str = content.decode(encoding, 'replace')
            try:
                jsondata = ujson.loads(content_str)
            except Exception as e:
                parse_errors = True
                returnval.add_error(e)
    returnval.update({ 'json': jsondata, 'parse_errors': parse_errors })
    return returnval

@shared_task
def parse_json_from_response(taskarg):
    """
    Task to parse json from a response.
    """
    if isinstance(taskarg, tuple):
        taskarg = taskarg[0]
    response_id = taskarg.get('response_id', None)
    response_info = taskarg.get('response_info', {})
    returnval = ResultDict(taskarg)
    response = URLInspection.objects.get(id=response_id)
    response_content = response.content.string()
    encoding = response.encoding if response.encoding else response.apparent_encoding
    result_dict = parse_json({'content':response_content, 'encoding':encoding})
    jsondata = result_dict.get('json', None)
    parse_errors = result_dict.get('parse_errors', False)
    if jsondata:
         returnval['json'] = jsondata
    response_info['json_errors'] = True if parse_errors else False
    response_info['is_json'] = True if jsondata else False
    errors = result_dict.get('errors', None)
    if errors:
        returnval.errors.extend(errors)
    returnval.get('response_info', {}).update(response_info)
    return returnval

@shared_task
def validate_json_catalog(taskarg):
    """
    Validate jsondata against the DATA_CATALOG_SCHEMA
    """
    if isinstance(taskarg, tuple):
        taskarg = taskarg[0]
    jsondata = taskarg.get('json', None)
    response_info = taskarg.get('response_info', {})
    returnval = ResultDict(taskarg)
    is_valid = False
    if jsondata and catalog_schema:
        is_valid = validator.is_valid(jsondata)
        if not is_valid:
            # Save up to SCHEMA_ERROR_LIMIT errors from schema validation
            error_iter = islice(validator.iter_errors(jsondata), SCHEMA_ERROR_LIMIT)
            for e in error_iter:
                returnval.add_error(e)
    response_info['is_valid_data_catalog'] = is_valid
    returnval.get('response_info', {}).update(response_info)
    returnval['report_type'] = Report.DATA_CATALOG_VALIDATION
    return returnval

@shared_task
def save_response_info(taskarg):
    report_id = taskarg.get('report_id', None)
    report_type = taskarg.get('report_type', Report.GENERIC_REPORT)
    response_id = taskarg.get('response_id', None)
    response_info = taskarg.get('response_info', {})
    returnval = ResultDict(taskarg)
    response_info.pop('content', None) # Let's not save content in our report
    response_info.pop('json', None) # Let's not save json in our report
    logger.info("Saving report info {0}".format(repr(response_info)))
    returnval['saved'] = False
    if response_info:
        if report_id:
            try:
                with transaction.atomic():
                    report = Report.objects.get(id=report_id)
                    report.report_type = report_type
                    report.save()
                    if response_id:
                        response = URLInspection.objects.get(id=response_id)
                        response.info.update(response_info)
                        if len(returnval.errors):
                            response.errors.extend(returnval.errors)
                        response.save()
                    returnval['saved'] = True
            except DatabaseError as e:
                raise e

@shared_task
def validate_data_catalogs():
    agencies = Agency.objects.all()
    groupchain = group([chain(
                    report_for_agency_url.subtask((agency.id, agency.data_json_url, Report.DATA_CATALOG_VALIDATION),
                                                  options={'link_error':error_handler.s()}),
                    parse_json_from_response.s(),
                    validate_json_catalog.s(),
                    save_response_info.s()
                ) for agency in agencies])
    return groupchain()

