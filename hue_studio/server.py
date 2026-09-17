import argparse
import copy
import json
import os
from pathlib import Path
import secrets
import threading
import time
import urllib.request
import webbrowser
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .bridge import Bridge, COLLECTIONS, HueError, clean, plan_restore, validate_snapshot

ROOT = Path(__file__).resolve().parent.parent
STATIC = Path(__file__).parent / 'static'


def private_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temp = path.with_suffix('.tmp')
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w') as out:
        json.dump(value, out, indent=2, ensure_ascii=False)
    os.chmod(temp, 0o600)
    os.replace(temp, path)


class Studio:
    def __init__(self, data_dir, export):
        self.directory = Path(data_dir)
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.lock = threading.Lock()
        self.bridge = None
        self.token = secrets.token_urlsafe(32)
        self.plans = {}
        self.data = {'config': {'name': 'Mi hogar'}, **{k: {} for k in COLLECTIONS}}
        if export.exists():
            self.data = validate_snapshot(json.loads(export.read_text()))
        self.profiles = {}
        profile_file = self.directory / 'bridges.json'
        if profile_file.exists():
            self.profiles = json.loads(profile_file.read_text())
        old = ROOT / '.hue-config.json'
        if old.exists():
            try:
                config = json.loads(old.read_text())
                if config.get('bridgeIp') and config.get('username'):
                    self.profiles.setdefault(config['bridgeIp'], {'key': config['username'], 'legacy': False, 'name': 'Bridge del proyecto'})
            except (ValueError, OSError):
                pass

    def state(self):
        return {'resources': clean(self.data), 'connected': self.bridge is not None,
                'host': self.bridge.host if self.bridge else None,
                'profiles': [{'host': host, 'name': p.get('name', host), 'legacy': p.get('legacy', False)} for host, p in self.profiles.items()]}

    def require_bridge(self):
        if not self.bridge:
            raise HueError('Conecta un bridge para realizar cambios. La exploración es de solo lectura.')
        return self.bridge

    def backup(self, label='manual'):
        bridge = self.require_bridge()
        data = bridge.snapshot(detailed=True)
        stamp = datetime.now(timezone.utc).isoformat()
        value = {'format': 'hue-studio', 'version': 1, 'created_at': stamp,
                 'bridge_id': data['config'].get('bridgeid'), 'label': label, 'resources': data}
        name = datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S') + '-' + secrets.token_hex(3) + '.json'
        private_json(self.directory / 'backups' / name, value)
        self.data = data
        return name

    def backups(self):
        result = []
        for p in sorted((self.directory / 'backups').glob('*.json'), reverse=True):
            try:
                value = json.loads(p.read_text())
                result.append({'file': p.name, 'created_at': value.get('created_at'),
                               'label': value.get('label', 'importado'), 'name': value['resources']['config'].get('name'),
                               'size': p.stat().st_size})
            except (OSError, ValueError, KeyError):
                continue
        return result

    def read_backup(self, name):
        if not isinstance(name, str) or Path(name).name != name or not name.endswith('.json'):
            raise HueError('Nombre de copia inválido.')
        return json.loads((self.directory / 'backups' / name).read_text())

    def act(self, route, body):
        if route == '/api/connect':
            host = body.get('host', '').strip()
            profile = self.profiles.get(host, {})
            bridge = Bridge(host, body.get('key') or profile.get('key', ''), bool(body.get('legacy', False)))
            if body.get('pair'):
                bridge.pair()
            if not bridge.key:
                raise HueError('Vincula el bridge con el botón físico o introduce una clave de aplicación.')
            data = bridge.snapshot()
            self.bridge, self.data = bridge, data
            self.plans.clear()
            self.profiles[host] = {'key': bridge.key, 'legacy': bridge.legacy, 'name': data['config'].get('name', host)}
            private_json(self.directory / 'bridges.json', self.profiles)
            return self.state()
        if route == '/api/disconnect':
            self.bridge = None
            self.plans.clear()
            return self.state()
        if route == '/api/refresh':
            self.data = self.require_bridge().snapshot()
            return self.state()
        if route == '/api/backup':
            return {'file': self.backup()}
        if route == '/api/import':
            resources = validate_snapshot(body.get('backup'))
            name = 'import-' + secrets.token_hex(8) + '.json'
            private_json(self.directory / 'backups' / name, {'format': 'hue-studio', 'version': 1,
                'created_at': datetime.now(timezone.utc).isoformat(), 'label': 'importado', 'resources': resources})
            return {'file': name}
        if route == '/api/preview':
            bridge = self.require_bridge()
            source = self.read_backup(body.get('file'))
            target = bridge.snapshot()
            plan = plan_restore(source, target)
            pid = secrets.token_urlsafe(24)
            self.plans = {pid: {'source': source, 'plan': plan, 'host': bridge.host, 'created': time.monotonic()}}
            return {**plan, 'plan_id': pid}
        if route == '/api/restore':
            bridge = self.require_bridge()
            stored = self.plans.pop(body.get('plan_id', ''), None)
            if not stored or stored['host'] != bridge.host or time.monotonic() - stored['created'] > 600:
                raise HueError('La vista previa ha caducado. Genera una nueva.')
            plan = plan_restore(stored['source'], bridge.snapshot())
            if plan != stored['plan']:
                raise HueError('La configuración destino ha cambiado. Revisa una nueva vista previa.')
            recovery = self.backup('antes de restaurar')
            if plan_restore(stored['source'], self.data) != plan:
                raise HueError('El destino cambió mientras se creaba la copia previa. Genera otra vista previa.')
            completed, mappings = [], {}
            error = None
            for operation in plan['operations']:
                payload = copy.deepcopy(operation['body'])
                if isinstance(payload.get('group'), str) and payload['group'].startswith('@group:'):
                    payload['group'] = mappings[payload['group']]
                try:
                    result = bridge.request(operation['method'], operation['path'], payload)
                    if operation['method'] == 'POST' and operation['kind'] == 'groups':
                        mappings['@group:' + operation['id']] = str(result[0]['success']['id'])
                    completed.append(operation)
                    time.sleep(.15)
                except (HueError, KeyError, IndexError, TypeError) as exc:
                    error = str(exc)
                    break
            report = {'completed': len(completed), 'total': len(plan['operations']), 'error': error,
                      'recovery_backup': recovery, 'skipped': plan['skipped']}
            private_json(self.directory / 'last-restore.json', report)
            try:
                self.data = bridge.snapshot()
            except HueError:
                report['refresh_error'] = 'No se pudo actualizar el estado; vuelve a conectar el bridge.'
            return report
        if route == '/api/resource':
            bridge = self.require_bridge()
            kind, rid = body.get('kind'), str(body.get('id', ''))
            method, payload = body.get('method', 'PUT'), body.get('data', {})
            section = body.get('section', '')
            if kind not in COLLECTIONS or not isinstance(payload, dict):
                raise HueError('Recurso inválido.')
            if method not in ('POST', 'PUT', 'DELETE'):
                raise HueError('Operación no admitida.')
            if method != 'POST' and rid not in self.data.get(kind, {}) and not (kind == 'groups' and rid == '0' and section == 'action'):
                raise HueError('El recurso ya no existe; actualiza la vista.')
            allowed_sections = {'lights': ('', 'state', 'config'), 'groups': ('', 'action'), 'sensors': ('', 'config')}
            if section not in allowed_sections.get(kind, ('',)):
                raise HueError('Propiedad no editable.')
            if method == 'POST' and kind not in ('groups', 'scenes', 'schedules', 'rules'):
                raise HueError('Empareja los dispositivos físicos con la aplicación Hue.')
            if method == 'DELETE' and kind not in ('groups', 'scenes', 'schedules', 'rules', 'resourcelinks'):
                raise HueError('La eliminación física de dispositivos no está habilitada.')
            if method == 'PUT' and not section and kind in ('lights', 'sensors') and set(payload) - {'name'}:
                raise HueError('Edita el nombre aquí; el estado y la configuración tienen su propio editor.')
            if method == 'DELETE':
                self.backup('antes de eliminar')
            path = '/' + kind + (('/' + rid) if method != 'POST' else '') + (('/' + section) if section else '')
            result = bridge.request(method, path, payload if method != 'DELETE' else None)
            try:
                self.data = bridge.snapshot()
            except HueError:
                return {'result': clean(result), 'warning': 'Cambio enviado. No se pudo actualizar la vista; pulsa Actualizar.'}
            return {'result': clean(result)}
        raise HueError('Operación desconocida.')


class Handler(BaseHTTPRequestHandler):
    server_version = 'HueStudio/1.0'

    def log_message(self, *_):
        pass

    @property
    def studio(self):
        return self.server.studio

    def send(self, status, data, content_type='application/json; charset=utf-8'):
        if not isinstance(data, bytes):
            data = json.dumps(data, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(data)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'")
        self.end_headers()
        self.wfile.write(data)

    def valid_host(self):
        return self.headers.get('Host') in (f'127.0.0.1:{self.server.server_port}', f'localhost:{self.server.server_port}')

    def do_GET(self):
        if not self.valid_host():
            self.send(403, {'error': 'Host no permitido.'})
            return
        path = urllib.parse.urlsplit(self.path).path
        try:
            if path == '/api/state':
                with self.studio.lock:
                    value = {**self.studio.state(), 'token': self.studio.token}
                self.send(200, value)
            elif path == '/api/backups':
                self.send(200, self.studio.backups())
            elif path.startswith('/api/download/'):
                self.send(200, self.studio.read_backup(path.removeprefix('/api/download/')))
            elif path == '/api/discover':
                with urllib.request.urlopen('https://discovery.meethue.com/', timeout=8) as response:
                    data = json.load(response)
                self.send(200, data)
            elif path in ('/', '/app.js', '/style.css'):
                name = {'/': 'index.html', '/app.js': 'app.js', '/style.css': 'style.css'}[path]
                mime = {'/': 'text/html', '/app.js': 'text/javascript', '/style.css': 'text/css'}[path]
                self.send(200, (STATIC / name).read_bytes(), mime + '; charset=utf-8')
            else:
                self.send(404, {'error': 'No encontrado.'})
        except Exception:
            self.send(400, {'error': 'No se pudo cargar el recurso. Para descubrir el bridge necesitas Internet; también puedes introducir su IP.'})

    def do_POST(self):
        origin = self.headers.get('Origin')
        allowed = (f'http://127.0.0.1:{self.server.server_port}', f'http://localhost:{self.server.server_port}')
        if not self.valid_host() or (origin and origin not in allowed) or not secrets.compare_digest(self.headers.get('X-Hue-Token', ''), self.studio.token):
            self.send(403, {'error': 'Sesión inválida. Recarga la página.'})
            return
        try:
            size = int(self.headers.get('Content-Length', '0'))
            if not 0 < size <= 20_000_000:
                raise HueError('El archivo debe tener menos de 20 MB.')
            body = json.loads(self.rfile.read(size))
            if not isinstance(body, dict):
                raise HueError('Solicitud inválida.')
            if not self.studio.lock.acquire(blocking=False):
                self.send(409, {'error': 'Hay otra operación en curso. Espera a que termine.'})
                return
            try:
                result = self.studio.act(self.path, body)
            finally:
                self.studio.lock.release()
            self.send(200, result)
        except (HueError, ValueError, FileNotFoundError) as exc:
            self.send(400, {'error': str(exc)})
        except Exception:
            self.send(500, {'error': 'No se pudo completar la operación. Actualiza el estado antes de reintentarlo.'})


def main():
    parser = argparse.ArgumentParser(description='Hue Studio · administración local de Philips Hue')
    parser.add_argument('--port', type=int, default=8765)
    parser.add_argument('--no-browser', action='store_true')
    parser.add_argument('--data-dir', type=Path, default=ROOT / '.hue-studio')
    parser.add_argument('--export', type=Path, default=ROOT / 'hue-export.json')
    args = parser.parse_args()
    studio = Studio(args.data_dir, args.export)
    server = ThreadingHTTPServer(('127.0.0.1', args.port), Handler)
    server.studio = studio
    url = f'http://127.0.0.1:{server.server_port}'
    print(f'Hue Studio → {url}\nCtrl+C para salir. Modo exploración hasta conectar un bridge.', flush=True)
    if not args.no_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
