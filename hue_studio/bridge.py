"""Hue v1 transport and conservative, reviewable restore planning."""
import copy
import ipaddress
import json
import re
import ssl
import urllib.error
import urllib.parse
import urllib.request

COLLECTIONS = ('lights', 'groups', 'scenes', 'sensors', 'schedules', 'rules', 'resourcelinks')


class HueError(Exception):
    pass


def clean(data):
    """Never send application credentials to the browser or backup files."""
    if isinstance(data, dict):
        return {k: clean(v) for k, v in data.items()
                if k.lower() not in ('whitelist', 'username', 'clientkey', 'applicationkey', 'password')}
    if isinstance(data, list):
        return [clean(v) for v in data]
    if isinstance(data, str):
        return re.sub(r'/api/[^/]+/', '/api/<application-key>/', data)
    return data


def validate_snapshot(value):
    if not isinstance(value, dict):
        raise HueError('Se necesita un backup Hue Studio o una exportación completa de la API v1.')
    data = value.get('resources', value)
    if not isinstance(data, dict) or not isinstance(data.get('lights'), dict) or not isinstance(data.get('config'), dict):
        raise HueError('Exportación inválida: faltan lights y config de la API v1.')
    for kind in COLLECTIONS:
        items = data.get(kind, {})
        if not isinstance(items, dict) or any(not isinstance(v, dict) for v in items.values()):
            raise HueError(f'Colección inválida: {kind}.')
        if any(not re.fullmatch(r'[A-Za-z0-9_-]+', str(k)) for k in items):
            raise HueError(f'Identificador inválido en {kind}.')
    return clean(copy.deepcopy(data))


class Bridge:
    def __init__(self, host, key='', legacy=False):
        try:
            address = ipaddress.ip_address(host)
        except ValueError as exc:
            raise HueError('Introduce la dirección IP local del bridge, sin http ni puerto.') from exc
        if not address.is_private or address.is_loopback or address.is_unspecified or address.is_multicast or address.is_link_local:
            raise HueError('Solo se admiten direcciones IP de una red privada local.')
        self.host, self.key, self.legacy = str(address), key, legacy
        hostpart = f'[{address}]' if address.version == 6 else str(address)
        self.base = ('http' if legacy else 'https') + '://' + hostpart
        # Hue uses a local certificate. This context is scoped to this private-IP bridge.
        context = ssl._create_unverified_context()
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}),
                                                 urllib.request.HTTPSHandler(context=context))

    def request(self, method, path='', body=None, pairing=False):
        key = urllib.parse.quote(self.key, safe='')
        url = self.base + ('/api' if pairing else '/api/' + key + path)
        request = urllib.request.Request(url, method=method,
            data=json.dumps(body).encode() if body is not None else None,
            headers={'Content-Type': 'application/json', 'Accept': 'application/json'})
        try:
            with self.opener.open(request, timeout=12) as response:
                result = json.load(response)
        except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
            raise HueError('No se pudo comunicar con el bridge. Comprueba IP, red, protocolo y autorización.') from exc
        errors = [r['error'] for r in result if isinstance(r, dict) and 'error' in r] if isinstance(result, list) else []
        if errors:
            if any(e.get('type') == 101 for e in errors):
                raise HueError('Pulsa el botón físico del bridge y vuelve a vincular en los próximos 30 segundos.')
            raise HueError('; '.join(str(e.get('description', e)) for e in errors))
        return result

    def pair(self):
        result = self.request('POST', body={'devicetype': 'hue-studio#python'}, pairing=True)
        try:
            self.key = result[0]['success']['username']
        except (KeyError, IndexError, TypeError) as exc:
            raise HueError('El bridge no devolvió una credencial válida.') from exc

    def snapshot(self, detailed=False):
        data = self.request('GET')
        if detailed:
            # The collection endpoint omits lightstates; fetch each scene for a restorable backup.
            import time
            for rid in data.get('scenes', {}):
                data['scenes'][rid] = self.request('GET', '/scenes/' + rid)
                time.sleep(.12)
        return validate_snapshot(data)


def light_state(state):
    result = {k: state[k] for k in ('on', 'bri') if k in state}
    mode = state.get('colormode')
    keys = {'ct': ('ct',), 'xy': ('xy',), 'hs': ('hue', 'sat')}.get(mode, ())
    if not mode:
        keys = ('xy',) if 'xy' in state else ('ct',) if 'ct' in state else ('hue', 'sat')
    result.update({k: state[k] for k in keys if k in state})
    return result


def plan_restore(source, target):
    """Match hardware by uniqueid; never assume numeric IDs survive migration."""
    source, target = validate_snapshot(source), validate_snapshot(target)
    operations, skipped = [], []
    light_map, group_map = {}, {}
    same = bool(source['config'].get('bridgeid')) and source['config'].get('bridgeid') == target['config'].get('bridgeid')

    def skip(kind, rid, reason):
        skipped.append({'kind': kind, 'id': rid, 'name': source.get(kind, {}).get(rid, {}).get('name', rid), 'reason': reason})

    def op(kind, rid, method, path, body):
        operations.append({'kind': kind, 'id': rid, 'name': source[kind][rid].get('name', rid),
                           'method': method, 'path': path, 'body': body})

    for kind in ('lights', 'sensors'):
        for rid, item in source.get(kind, {}).items():
            matches = [tid for tid, t in target.get(kind, {}).items()
                       if item.get('uniqueid') and item.get('uniqueid') == t.get('uniqueid')]
            if len(matches) != 1:
                skip(kind, rid, 'Sin coincidencia física única. Empareja el dispositivo en el bridge destino.')
                continue
            tid = matches[0]
            if kind == 'lights':
                light_map[rid] = tid
            if item.get('name'):
                op(kind, rid, 'PUT', f'/{kind}/{tid}', {'name': item['name']})
            if kind == 'lights':
                state = light_state(item.get('state', {}))
                if state:
                    op(kind, rid, 'PUT', f'/lights/{tid}/state', state)
            else:
                config = {k: item['config'][k] for k in ('on', 'ledindication', 'sensitivity', 'usertest') if k in item.get('config', {})}
                if config:
                    op(kind, rid, 'PUT', f'/sensors/{tid}/config', config)

    for rid, item in source.get('groups', {}).items():
        if item.get('type') not in ('Room', 'Zone', 'LightGroup'):
            skip('groups', rid, 'Grupo especial/Entertainment: configuración manual necesaria.')
            continue
        if any(lid not in light_map for lid in item.get('lights', [])) or item.get('sensors'):
            skip('groups', rid, 'Hay luces sin correspondencia o sensores asociados; no se restaura parcialmente.')
            continue
        matches = [tid for tid, t in target.get('groups', {}).items()
                   if t.get('type') == item.get('type') and t.get('name') == item.get('name')]
        if same and rid in target.get('groups', {}) and target['groups'][rid].get('type') == item.get('type'):
            matches = [rid]
        if len(matches) > 1:
            skip('groups', rid, 'Nombre duplicado en destino: coincidencia ambigua.')
            continue
        payload = {'name': item.get('name', 'Grupo'), 'lights': [light_map[lid] for lid in item.get('lights', [])]}
        if item.get('type') in ('Room', 'Zone') and 'class' in item:
            payload['class'] = item['class']
        if matches:
            group_map[rid] = matches[0]
            op('groups', rid, 'PUT', '/groups/' + matches[0], payload)
        else:
            group_map[rid] = '@group:' + rid
            op('groups', rid, 'POST', '/groups', {**payload, 'type': item['type']})

    for rid, item in source.get('scenes', {}).items():
        states = item.get('lightstates')
        lights = item.get('lights', list(states or {}))
        if not states or any(lid not in light_map for lid in set(lights) | set(states)):
            skip('scenes', rid, 'Faltan estados de luz o dispositivos. Crea una copia completa desde la aplicación.')
            continue
        payload = {'name': item.get('name', 'Escena'), 'recycle': False,
                   'lightstates': {light_map[lid]: light_state(state) for lid, state in states.items()}}
        if item.get('type') == 'GroupScene':
            if item.get('group') not in group_map:
                skip('scenes', rid, 'No se puede resolver el grupo de la escena.')
                continue
            payload.update(type='GroupScene', group=group_map[item['group']])
        else:
            payload.update(type='LightScene', lights=[light_map[lid] for lid in lights])
        # New scenes avoid overwriting locked scenes or scenes owned by other apps.
        op('scenes', rid, 'POST', '/scenes', payload)

    for kind in ('schedules', 'rules', 'resourcelinks'):
        for rid in source.get(kind, {}):
            skip(kind, rid, 'Incluido en la copia; requiere revisar referencias y credenciales manualmente.')
    return {'operations': operations, 'skipped': skipped, 'same_bridge': same,
            'notes': ['No elimina recursos existentes. Las escenas se crean como copias nuevas.',
                      'Restaura nombres, estados de luces, ajustes básicos de sensores y grupos compatibles.',
                      'No restaura emparejamientos Zigbee, usuarios, red, integraciones ni automatizaciones.',
                      'La API no es transaccional: un error detiene el proceso; puede haber cambios parciales.']}
