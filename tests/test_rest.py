import datetime
import json

import httpx2
import pytest

import webuntis
from webuntis.utils.rest import (RESOURCE_TYPES, RestClient, merge_period_texts,
                                 _iso_date)
from . import stub_session_parameters

SERVER = 'https://foo.webuntis.com/WebUntis/jsonrpc.do'

HTML = '''<script>var dojoConfig = {
  "grupet": {"csrfHeader":"X-CSRF-TOKEN","csrfToken":"tok123","user":{"id":1}}
};</script>'''


def make_client(handler, **overrides):
    params = dict(stub_session_parameters, server=SERVER)
    params.update(overrides)
    params['_http_session'] = httpx2.Client(transport=httpx2.MockTransport(handler))
    return RestClient(webuntis.Session(**params).config)


def test_iso_date():
    assert _iso_date(datetime.date(2026, 11, 9)) == '2026-11-09'
    assert _iso_date(datetime.datetime(2026, 11, 9, 8, 0)) == '2026-11-09'
    assert _iso_date(20261109) == '2026-11-09'


def test_urls_derived_from_server():
    client = make_client(lambda request: httpx2.Response(200, text=HTML))
    assert client.urls == ('https://foo.webuntis.com', '/WebUntis')


def test_csrf_scraped_and_cached():
    calls = []

    def handler(request):
        calls.append(str(request.url))
        return httpx2.Response(200, text=HTML)

    client = make_client(handler)
    assert client.csrf() == ('X-CSRF-TOKEN', 'tok123')
    assert client.csrf() == ('X-CSRF-TOKEN', 'tok123')
    assert calls == ['https://foo.webuntis.com/WebUntis/']


def test_csrf_missing_token():
    client = make_client(lambda request: httpx2.Response(200, text='<html/>'))
    with pytest.raises(webuntis.errors.RemoteError):
        client.csrf()


def test_bearer_cached():
    calls = []

    def handler(request):
        calls.append(str(request.url))
        return httpx2.Response(200, text='jwt-token\n')

    client = make_client(handler)
    assert client.bearer() == 'jwt-token'
    assert client.bearer() == 'jwt-token'
    assert calls == ['https://foo.webuntis.com/WebUntis/api/token/new']


def test_timetable_entries_request():
    seen = {}

    def handler(request):
        url = str(request.url)
        if url.endswith('/WebUntis/'):
            return httpx2.Response(200, text=HTML)
        if url.endswith('/api/token/new'):
            return httpx2.Response(200, text='jwt-token')
        seen['url'] = url
        seen['headers'] = dict(request.headers)
        return httpx2.Response(200, json={'days': [{'date': '2026-11-09'}]})

    client = make_client(handler)
    days = client.timetable_entries(RESOURCE_TYPES[1], 731,
                                    datetime.date(2026, 11, 9),
                                    datetime.date(2026, 11, 9))
    assert days == [{'date': '2026-11-09'}]
    assert seen['url'] == (
        'https://foo.webuntis.com/WebUntis/api/rest/view/v1/timetable/entries'
        '?start=2026-11-09&end=2026-11-09&resourceType=CLASS&resources=731&format=1')
    assert seen['headers']['authorization'] == 'Bearer jwt-token'
    assert seen['headers']['x-csrf-token'] == 'tok123'
    assert seen['headers']['cookie'] == 'JSESSIONID=FOOBOO_SESSION'


@pytest.mark.parametrize('status,exc', [
    (401, webuntis.errors.NotLoggedInError),
    (403, webuntis.errors.RemoteError),
    (400, webuntis.errors.DateNotAllowed),
    (500, webuntis.errors.RemoteError),
])
def test_error_mapping(status, exc):
    def handler(request):
        url = str(request.url)
        if url.endswith('/WebUntis/'):
            return httpx2.Response(200, text=HTML)
        if url.endswith('/api/token/new'):
            return httpx2.Response(200, text='jwt-token')
        return httpx2.Response(status, json={'errorCode': 'X'})

    client = make_client(handler)
    with pytest.raises(exc):
        client.timetable_entries('CLASS', 731, 20261109, 20261109)


def test_merge_fills_gaps_only():
    periods = [
        {'id': 1, 'info': 'from jsonrpc', 'startTime': 800},
        {'id': 2, 'startTime': 845},
        {'id': 3, 'startTime': 950},
    ]
    days = [{
        'date': '2026-11-09',
        'gridEntries': [
            {
                'ids': [1],
                'type': 'NORMAL_TEACHING_PERIOD',
                'texts': [
                    {'type': 'NOTES_FOR_ALL', 'text': 'rest notes'},
                    {'type': 'LESSON_INFO', 'text': 'rest lesson info'},
                ],
            },
            {
                'ids': [2],
                'type': 'EXAM',
                'texts': [{'type': 'LESSON_INFO', 'text': 'LEK Plattentektonik'}],
            },
        ],
    }]

    merged = merge_period_texts(periods, days)

    assert merged[0]['info'] == 'from jsonrpc'
    assert merged[0]['lstext'] == 'rest lesson info'
    assert merged[0]['lessonInfo'] == 'rest lesson info'
    assert merged[0]['exam'] is False

    assert merged[1]['lstext'] == 'LEK Plattentektonik'
    assert merged[1]['lessonInfo'] == 'LEK Plattentektonik'
    assert merged[1]['exam'] is True

    # periods without a REST entry are left alone apart from the new defaults
    assert merged[2]['lessonInfo'] == ''
    assert merged[2]['exam'] is False

    # the input is not mutated
    assert 'lstext' not in periods[0]
    assert 'exam' not in periods[1]


def test_merge_ignores_empty_texts():
    periods = [{'id': 1}]
    days = [{'gridEntries': [{'ids': [1], 'type': 'EXAM',
                              'texts': [{'type': 'LESSON_INFO', 'text': ''}]}]}]
    merged = merge_period_texts(periods, days)
    assert 'lstext' not in merged[0]
    assert merged[0]['lessonInfo'] == ''
    assert merged[0]['exam'] is True


def test_period_object_properties():
    session = webuntis.Session(**stub_session_parameters)
    period = webuntis.objects.PeriodObject(
        session=session, data={'id': 1, 'lessonInfo': 'LEK', 'exam': True})
    assert period.lesson_info == 'LEK'
    assert period.exam is True

    plain = webuntis.objects.PeriodObject(session=session, data={'id': 2})
    assert plain.lesson_info == ''
    assert plain.exam is False
