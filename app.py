import argparse
from datetime import date
from html import escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
import time
from urllib.parse import urlencode
from urllib.request import Request, urlopen


def env(name, alias=None):
    return os.environ.get(name) or os.environ[alias or name]


def request(url, headers, payload=None, method=None):
    body = None if payload is None else json.dumps(payload).encode()
    with urlopen(Request(url, data=body, headers=headers, method=method), timeout=20) as response:
        raw = response.read()
    return json.loads(raw) if raw else None


def database(query, payload=None, method=None):
    secret = env('SUPABASE_SECRET_KEY', 'supa_secret_key')
    headers = {'apikey': secret, 'Content-Type': 'application/json',
               'Prefer': 'resolution=merge-duplicates,return=minimal'}
    if secret.startswith('eyJ'):
        headers['Authorization'] = 'Bearer ' + secret
    base = env('SUPABASE_URL').rstrip('/')
    return request(base + '/rest/v1/j1_match_import?' + query, headers, payload, method)


def provider(path, **params):
    url = 'https://v3.football.api-sports.io/' + path + '?' + urlencode(params)
    result = request(url, {'x-apisports-key': env('API_FOOTBALL_KEY', 'api_football')})
    if result['errors']:
        raise RuntimeError('API-Football rejected the request: ' + str(result['errors']))
    if result.get('paging', {}).get('total', 1) > 1:
        raise RuntimeError('Choose a smaller date range.')
    return result['response']


def run_import(args):
    fixtures = provider('fixtures', league=98, season=args.season, timezone='Asia/Tokyo',
                        **{'from': args.start.isoformat(), 'to': args.end.isoformat()})
    if not fixtures or len(fixtures) > 20:
        raise RuntimeError('Choose a date range with 1 to 20 fixtures.')
    rows = []
    for match in fixtures:
        fixture = match['fixture']
        rows.append(dict(fixture_id=fixture['id'], season=match['league']['season'],
                         round=match['league']['round'], kickoff_at=fixture['date'],
                         status=fixture['status']['short'], home_team=match['teams']['home']['name'],
                         away_team=match['teams']['away']['name'], home_goals=match['goals']['home'],
                         away_goals=match['goals']['away'], fixture=match))
    if args.write:
        database('on_conflict=fixture_id', rows)
    print(f'Fixtures: {len(rows)}', flush=True)
    for row in rows:
        if row['status'] not in {'FT', 'AET', 'PEN'}:
            continue
        detail_types = [('statistics', 'team_stats')]
        if args.players:
            detail_types.append(('players', 'player_stats'))
        for endpoint, column in detail_types:
            time.sleep(8)
            fixture_id = row['fixture_id']
            stats = provider('fixtures/' + endpoint, fixture=fixture_id)
            if stats and args.write:
                database(f'fixture_id=eq.{fixture_id}', {column: stats}, 'PATCH')
            print(f'{fixture_id} {column}: {len(stats)} records', flush=True)


class ResultsHandler(BaseHTTPRequestHandler):
    def send_page(self, code, content, content_type='text/plain'):
        body = content.encode()
        self.send_response(code)
        self.send_header('Content-Type', content_type + '; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path != '/':
            self.send_page(404, 'Page not found')
            return
        columns = ['kickoff_at', 'home_team', 'home_goals', 'away_goals', 'away_team']
        try:
            matches = database('select=' + ','.join(columns) + '&order=kickoff_at.desc&limit=100')
        except (OSError, KeyError, ValueError) as error:
            self.log_error('Supabase read failed (%s)', type(error).__name__)
            self.send_page(503, 'Match results are temporarily unavailable.')
            return
        print(f'Supabase returned {len(matches)} J1 fixtures.', flush=True)
        rows = ''.join('<tr>' + ''.join('<td>' + escape(str(match[c]) if match[c] is not None
                       else '–') + '</td>' for c in columns) + '</tr>' for match in matches)
        page = ('<!doctype html><html lang="en"><meta charset="utf-8">'
                '<meta name="viewport" content="width=device-width, initial-scale=1">'
                '<title>J1 League results</title><style>body{font:16px system-ui;margin:2rem}'
                'td,th{padding:.45rem;text-align:left;border-bottom:1px solid #ddd}</style>'
                '<h1>J1 League results</h1><table><thead><tr><th>Kickoff</th><th>Home</th>'
                '<th>Goals</th><th>Goals</th><th>Away</th></tr></thead><tbody>' +
                (rows or '<tr><td colspan="5">No matches imported yet.</td></tr>') + '</tbody></table></html>')
        self.send_page(200, page, 'text/html')


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest='command')
    importer = commands.add_parser('import')
    importer.add_argument('--season', type=int, required=True)
    importer.add_argument('--from', dest='start', type=date.fromisoformat, required=True)
    importer.add_argument('--to', dest='end', type=date.fromisoformat, required=True)
    importer.add_argument('--write', action='store_true')
    importer.add_argument('--players', action='store_true')
    args = parser.parse_args()
    if args.command == 'import':
        if args.start > args.end:
            parser.error('--from must be earlier than or equal to --to')
        run_import(args)
    else:
        ThreadingHTTPServer(('0.0.0.0', int(os.environ.get('PORT', '8080'))), ResultsHandler).serve_forever()
