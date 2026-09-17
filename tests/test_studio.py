import copy
import io
import json
from pathlib import Path
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from unittest.mock import patch

from hue_studio.bridge import Bridge, HueError, clean, plan_restore, validate_snapshot
from hue_studio.server import Handler, Studio, ThreadingHTTPServer


def snapshot(bridge='A', light_id='1'):
    return {'config': {'bridgeid': bridge, 'name': 'Casa'},
            'lights': {light_id: {'name': 'Lámpara', 'uniqueid': 'hardware-1', 'state':
                        {'on': True, 'bri': 150, 'colormode': 'ct', 'ct': 300, 'xy': [.2, .3], 'reachable': True}}},
            'groups': {'2': {'name': 'Salón', 'type': 'Room', 'class': 'Living room', 'lights': [light_id]}},
            'scenes': {'scene1': {'name': 'Lectura', 'type': 'GroupScene', 'group': '2',
                       'lights': [light_id], 'lightstates': {light_id: {'on': True, 'bri': 120}}}},
            'sensors': {}, 'rules': {}, 'schedules': {}, 'resourcelinks': {}}


class FakeBridge:
    host = '192.168.1.2'

    def __init__(self, data, fail=False):
        self.data, self.calls, self.fail = data, [], fail

    def snapshot(self, detailed=False):
        return copy.deepcopy(self.data)

    def request(self, method, path, body):
        self.calls.append((method, path, body))
        if self.fail:
            raise HueError('fallo simulado')
        return [{'success': {'id': '99'}}]


class RestoreTests(unittest.TestCase):
    def test_transport_detects_errors_inside_http_success(self):
        bridge = Bridge('192.168.1.2', 'private-key')
        response = io.BytesIO(json.dumps([{'success': {'/lights/1/name': 'Luz'}},
                                         {'error': {'type': 7, 'description': 'invalid value'}}]).encode())
        with patch.object(bridge.opener, 'open', return_value=response):
            with self.assertRaisesRegex(HueError, 'invalid value'):
                bridge.request('PUT', '/lights/1', {'name': 'Luz'})

    def test_backup_fetches_individual_scene_states(self):
        bridge = Bridge('192.168.1.2', 'private-key')
        collection = snapshot()
        scene = collection['scenes']['scene1'].copy()
        del collection['scenes']['scene1']['lightstates']
        with patch.object(bridge, 'request', side_effect=[collection, scene]) as request:
            result = bridge.snapshot(detailed=True)
        self.assertIn('lightstates', result['scenes']['scene1'])
        self.assertEqual(request.call_args.args, ('GET', '/scenes/scene1'))

    def test_remaps_physical_lights_and_new_groups(self):
        source, target = snapshot(), snapshot('B', '77')
        target['groups'] = {}
        plan = plan_restore(source, target)
        state = next(o for o in plan['operations'] if o['path'].endswith('/state'))
        self.assertEqual(state['path'], '/lights/77/state')
        self.assertEqual(state['body'], {'on': True, 'bri': 150, 'ct': 300})
        group = next(o for o in plan['operations'] if o['kind'] == 'groups')
        self.assertEqual(group['body']['lights'], ['77'])
        scene = next(o for o in plan['operations'] if o['kind'] == 'scenes')
        self.assertEqual(scene['body']['group'], '@group:2')
        self.assertEqual(list(scene['body']['lightstates']), ['77'])

    def test_missing_physical_device_never_matches_numeric_id(self):
        target = snapshot('B')
        target['lights']['1']['uniqueid'] = 'other-hardware'
        plan = plan_restore(snapshot(), target)
        self.assertEqual(plan['operations'], [])
        self.assertEqual(len(plan['skipped']), 3)

    def test_incomplete_scenes_skipped(self):
        source = snapshot()
        del source['scenes']['scene1']['lightstates']
        plan = plan_restore(source, snapshot())
        self.assertFalse(any(o['kind'] == 'scenes' for o in plan['operations']))
        self.assertEqual(plan['skipped'][0]['kind'], 'scenes')

    def test_ambiguous_groups_not_overwritten(self):
        target = snapshot('B')
        target['groups']['3'] = copy.deepcopy(target['groups']['2'])
        plan = plan_restore(snapshot(), target)
        self.assertFalse(any(o['kind'] in ('groups', 'scenes') for o in plan['operations']))

    def test_secrets_removed_and_original_unchanged(self):
        value = snapshot()
        value['config']['whitelist'] = {'secret': {'name': 'app'}}
        value['rules']['1'] = {'actions': [{'address': '/api/my-secret/lights/1/state'}]}
        cleaned = clean(value)
        self.assertNotIn('whitelist', cleaned['config'])
        self.assertIn('whitelist', value['config'])
        self.assertNotIn('my-secret', json.dumps(cleaned))

    def test_bad_backup_and_host_rejected(self):
        for value in ([], {}, {'lights': [], 'config': {}}):
            with self.assertRaises(HueError):
                validate_snapshot(value)
        for host in ('example.com', '127.0.0.1', '8.8.8.8', '192.168.1.1/api', '169.254.169.254'):
            with self.assertRaises(HueError):
                Bridge(host)


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.studio = Studio(self.root, self.root / 'missing.json')
        target = snapshot('B', '77')
        target['groups'] = {}
        self.studio.bridge = FakeBridge(target)
        self.studio.data = target
        self.file = self.studio.act('/api/import', {'backup': snapshot()})['file']

    def tearDown(self):
        self.temp.cleanup()

    def test_preview_does_not_write_restore_backs_up_and_resolves_groups(self):
        plan = self.studio.act('/api/preview', {'file': self.file})
        self.assertEqual(self.studio.bridge.calls, [])
        result = self.studio.act('/api/restore', {'plan_id': plan['plan_id']})
        self.assertIsNone(result['error'])
        self.assertTrue((self.root / 'backups' / result['recovery_backup']).exists())
        scene = self.studio.bridge.calls[-1]
        self.assertEqual(scene[2]['group'], '99')
        with self.assertRaises(HueError):
            self.studio.act('/api/restore', {'plan_id': plan['plan_id']})

    def test_failure_stops_and_keeps_recovery(self):
        plan = self.studio.act('/api/preview', {'file': self.file})
        self.studio.bridge.fail = True
        result = self.studio.act('/api/restore', {'plan_id': plan['plan_id']})
        self.assertEqual(result['completed'], 0)
        self.assertEqual(len(self.studio.bridge.calls), 1)
        self.assertTrue((self.root / 'backups' / result['recovery_backup']).exists())

    def test_backup_failure_prevents_any_restore_write(self):
        plan = self.studio.act('/api/preview', {'file': self.file})
        with patch.object(self.studio, 'backup', side_effect=OSError('disk full')):
            with self.assertRaises(OSError):
                self.studio.act('/api/restore', {'plan_id': plan['plan_id']})
        self.assertEqual(self.studio.bridge.calls, [])

    def test_changed_target_rejects_plan(self):
        plan = self.studio.act('/api/preview', {'file': self.file})
        self.studio.bridge.data['lights']['77']['uniqueid'] = 'replacement'
        with self.assertRaises(HueError):
            self.studio.act('/api/restore', {'plan_id': plan['plan_id']})
        self.assertEqual(self.studio.bridge.calls, [])

    def test_backup_paths_cannot_escape(self):
        with self.assertRaises(HueError):
            self.studio.read_backup('../bridges.json')


class HttpTests(unittest.TestCase):
    def test_local_server_static_readonly_and_csrf(self):
        with tempfile.TemporaryDirectory() as tmp:
            studio = Studio(Path(tmp), Path(tmp) / 'absent')
            server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
            server.studio = studio
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            base = f'http://127.0.0.1:{server.server_port}'
            try:
                with urllib.request.urlopen(base) as response:
                    self.assertIn(b'Hue Studio', response.read())
                with urllib.request.urlopen(base + '/i18n.js') as response:
                    self.assertIn(b'function translate', response.read())
                with urllib.request.urlopen(base + '/api/state') as response:
                    state = json.load(response)
                    self.assertFalse(state['connected'])
                bad = urllib.request.Request(base + '/api/disconnect', data=b'{}', method='POST')
                with self.assertRaises(urllib.error.HTTPError) as error:
                    urllib.request.urlopen(bad)
                self.assertEqual(error.exception.code, 403)
                error.exception.close()
                request = urllib.request.Request(base + '/api/backup', data=b'{}', method='POST',
                                                 headers={'X-Hue-Token': state['token']})
                with self.assertRaises(urllib.error.HTTPError) as error:
                    urllib.request.urlopen(request)
                self.assertEqual(error.exception.code, 400)
                error.exception.close()
                hostile = urllib.request.Request(base + '/api/state', headers={'Host': 'attacker.example'})
                with self.assertRaises(urllib.error.HTTPError) as error:
                    urllib.request.urlopen(hostile)
                self.assertEqual(error.exception.code, 403)
                error.exception.close()
            finally:
                server.shutdown()
                server.server_close()
                thread.join()


if __name__ == '__main__':
    unittest.main()
