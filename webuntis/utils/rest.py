"""
    This file is part of python-webuntis

    :copyright: (c) 2013 by Markus Unterwaditzer.
    :license: BSD, see LICENSE for more details.

    REST v1 helpers.

    The JSON-RPC endpoint (``.../WebUntis/jsonrpc.do``) does not return
    everything the WebUntis web app and mobile app show. In particular the
    per-lesson "Information zur Stunde" (``LESSON_INFO``) is missing for
    periods Untis flags as an exam, and exam periods themselves never show up
    in ``getTimetable`` at all. Those come from the REST v1 API, which is what
    this module talks to.

    The REST API shares the JSON-RPC session's ``JSESSIONID`` cookie but
    additionally expects a CSRF token (scraped from the web app's HTML) and a
    short lived JWT from ``api/token/new``.
"""

from __future__ import unicode_literals

import re
import time

import httpx2

from webuntis import errors
from webuntis.utils import datetime_utils, log
from webuntis.utils.third_party import urlparse

#: JWT lifetime is 15 minutes; refresh well before it expires.
TOKEN_TTL = 600.0

_CSRF_HEADER_RE = re.compile(r'"csrfHeader"\s*:\s*"([^"]*)"')
_CSRF_TOKEN_RE = re.compile(r'"csrfToken"\s*:\s*"([^"]*)"')

_ACCEPT = 'application/json, text/plain, */*'

#: REST v1 resource names, keyed by the JSON-RPC element type number.
RESOURCE_TYPES = {
    1: 'CLASS',
    2: 'TEACHER',
    3: 'SUBJECT',
    4: 'ROOM',
    5: 'STUDENT',
}

#: REST v1 text type -> raw ``getTimetable`` field it fills.
TEXT_FIELDS = {
    'NOTES_FOR_ALL': 'info',
    'LESSON_INFO': 'lstext',
    'SUBSTITUTION_TEXT': 'substText',
}


def _iso_date(value):
    """Format a date/datetime/``YYYYMMDD`` int as an ISO date string."""
    raw = str(datetime_utils.format_date(value))
    return raw[:4] + '-' + raw[4:6] + '-' + raw[6:8]


class RestClient(object):
    """Minimal client for the WebUntis REST v1 API.

    Reuses the cookies and HTTP session of a logged in
    :py:class:`webuntis.Session`, so it must only be created after
    :py:meth:`webuntis.Session.login`.
    """

    def __init__(self, config):
        self.config = config
        self._csrf = None
        self._token = None
        self._token_at = 0.0

    @property
    def http(self):
        if '_http_session' not in self.config:
            self.config['_http_session'] = httpx2.Client()
        return self.config['_http_session']

    @property
    def urls(self):
        """``(root, context)``, e.g. ``('https://host', '/WebUntis')``."""
        split = urlparse.urlparse(self.config['server'])
        root = split.scheme + '://' + split.netloc
        path = split.path or ''
        context = path.rsplit('/', 1)[0] if '/' in path else ''
        return root, context or '/WebUntis'

    def _headers(self, extra=None):
        root, context = self.urls
        headers = {
            'Accept': _ACCEPT,
            'Referer': root + context + '/',
            'User-Agent': self.config['useragent'],
        }
        if 'jsessionid' in self.config:
            headers['Cookie'] = 'JSESSIONID=' + self.config['jsessionid']
        headers.update(extra or {})
        return headers

    def _get(self, path, params=None, headers=None):
        root, context = self.urls
        url = root + context + path
        log('debug', 'REST GET: ' + url)
        response = self.http.get(url, params=params,
                                 headers=self._headers(headers))
        if response.status_code == 401:
            raise errors.NotLoggedInError(
                'REST API rejected the session for ' + url)
        if response.status_code == 400:
            raise errors.DateNotAllowed(
                'REST API rejected the request for ' + url + ': ' +
                response.text[:200])
        if response.status_code >= 400:
            raise errors.RemoteError(
                'REST API returned %s for %s: %s' %
                (response.status_code, url, response.text[:200]))
        try:
            return response.json()
        except ValueError:
            raise errors.RemoteError('REST API returned invalid JSON for ' +
                                     url, response.text[:200])

    def csrf(self):
        """The web app's ``(header name, token)`` pair, scraped from its HTML."""
        if self._csrf is None:
            root, context = self.urls
            response = self.http.get(root + context + '/',
                                     headers=self._headers())
            if response.status_code >= 400:
                raise errors.RemoteError(
                    'Could not load the WebUntis web app: HTTP %s' %
                    response.status_code)
            header = _CSRF_HEADER_RE.search(response.text)
            token = _CSRF_TOKEN_RE.search(response.text)
            if not header or not token:
                raise errors.RemoteError(
                    'No CSRF token found in the WebUntis web app HTML')
            self._csrf = (header.group(1), token.group(1))
        return self._csrf

    def bearer(self):
        """A JWT for the REST API, minted from ``api/token/new``."""
        now = time.monotonic()
        if self._token is None or now - self._token_at > TOKEN_TTL:
            root, context = self.urls
            response = self.http.get(root + context + '/api/token/new',
                                     headers=self._headers())
            if response.status_code >= 400:
                raise errors.RemoteError(
                    'Could not mint a REST API token: HTTP %s' %
                    response.status_code)
            token = response.text.strip()
            try:
                token = response.json().get('token', token)
            except (ValueError, AttributeError):
                pass
            if not token:
                raise errors.RemoteError('REST API token response was empty')
            self._token = token
            self._token_at = now
        return self._token

    def timetable_entries(self, resource_type, resource_id, start, end):
        """Fetch the REST v1 timetable entries of one element for a range.

        :param resource_type: one of :py:data:`RESOURCE_TYPES`' values, e.g.
            ``"CLASS"``
        :param resource_id: the element's id
        :param start: first day of the range
        :param end: last day of the range
        :return: the ``days`` list of the REST response
        """
        header, token = self.csrf()
        days = self._get(
            '/api/rest/view/v1/timetable/entries',
            params={
                'start': _iso_date(start),
                'end': _iso_date(end),
                'resourceType': resource_type,
                'resources': str(resource_id),
                'format': 1,
            },
            headers={header: token, 'Authorization': 'Bearer ' + self.bearer()},
        ).get('days') or []
        return days


def merge_period_texts(periods, days):
    """Merge the REST v1 texts and exam flag into raw ``getTimetable`` periods.

    ``periods`` is the list of dicts returned by the JSON-RPC ``getTimetable``
    call, ``days`` the ``days`` list of the REST v1 timetable response. Entries
    are matched by period id. Values already present from JSON-RPC win; the
    REST values only fill gaps, because the JSON-RPC API returns nothing at all
    for some periods (notably exams).

    Each period additionally gets ``lessonInfo`` (the "Information zur Stunde")
    and ``exam`` (whether Untis flags the period as an exam).
    """
    by_id = {}
    for day in days:
        for entry in day.get('gridEntries') or []:
            for period_id in entry.get('ids') or []:
                by_id[period_id] = entry

    merged = []
    for period in periods:
        period = dict(period)
        entry = by_id.get(period.get('id'))
        texts = {}
        for text in (entry or {}).get('texts') or []:
            value = text.get('text') or ''
            if value:
                texts.setdefault(text.get('type'), value)
        for text_type, field in TEXT_FIELDS.items():
            if texts.get(text_type) and not period.get(field):
                period[field] = texts[text_type]
        period['lessonInfo'] = texts.get('LESSON_INFO', '')
        period['exam'] = (entry or {}).get('type') == 'EXAM'
        merged.append(period)
    return merged
