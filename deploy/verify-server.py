"""Deployment checks using private settings; never print credentials."""
import argparse
import secrets
from pathlib import Path
from urllib.parse import parse_qs, urlparse, urlunsplit

import requests
from dotenv import dotenv_values

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--base-url', help='Public HTTPS URL to check')
args = parser.parse_args()
settings = dotenv_values(Path(__file__).resolve().parents[1] / '.env')
for key in ('APP_PASSWORD', 'STRAVA_CLIENT_ID', 'STRAVA_REDIRECT_URI'):
    if not settings.get(key):
        parser.error(f'Configure {key} in private .env before running this check.')
callback = urlparse(settings['STRAVA_REDIRECT_URI'])
base = (args.base_url or urlunsplit((callback.scheme, callback.netloc, '', '', ''))).rstrip('/')
if urlparse(base).scheme != 'https':
    parser.error('Deployment checks require HTTPS.')

assert requests.get(base + '/health', timeout=15).json() == {'ok': True}
assert requests.get(base + '/api/state', timeout=15).status_code == 401
client = requests.Session()
response = client.post(base + '/login', data={'password': settings['APP_PASSWORD']}, timeout=15)
assert response.status_code == 200 and '检查运动记录' in response.text
assert client.get(base + '/api/state', timeout=15).status_code == 200
authorization = client.get(base + '/auth/strava', allow_redirects=False, timeout=15)
assert authorization.status_code == 302
query = parse_qs(urlparse(authorization.headers['Location']).query)
assert query['client_id'] == [settings['STRAVA_CLIENT_ID']]
assert query['scope'] == ['activity:write']
assert query['redirect_uri'] == [settings['STRAVA_REDIRECT_URI']]
invalid_state = secrets.token_urlsafe(24)
response = client.get(base + '/', params={'code': 'invalid', 'state': invalid_state}, timeout=15)
assert response.status_code == 200
assert parse_qs(urlparse(response.url).query).get('error') == ['Strava 授权校验失败，请重新连接。']
print('PASS: HTTPS, protected API, login, account-state API, OAuth parameters and invalid-state handling.')
print('No Keep credentials submitted and no Strava authorization/upload performed.')
