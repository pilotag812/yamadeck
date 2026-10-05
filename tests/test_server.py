"""End-to-end checks against a fake receiver; never sends PUT to real hardware."""
import json
from concurrent.futures import ThreadPoolExecutor
import sys
import threading
import tempfile
import unittest
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from server import Handler, Receiver

FIXTURES = Path(__file__).parent / 'fixtures'


class FakeReceiver(BaseHTTPRequestHandler):
    commands = []
    reject = False
    list_entered = None
    list_release = None
    entries = None
    selected_index = None
    state = ET.parse(FIXTURES / 'status.xml').getroot()
    server_list = ET.parse(FIXTURES / 'server-list_info.xml').getroot()
    server_play = ET.parse(FIXTURES / 'server-play_info.xml').getroot()
    tuner_play = ET.parse(FIXTURES / 'tuner-play.xml').getroot()

    def send_xml(self, body):
        self.send_response(200)
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        self.send_xml((FIXTURES / 'desc.xml').read_bytes())

    def do_POST(self):
        root = ET.fromstring(self.rfile.read(int(self.headers['Content-Length'])))
        if root.get('cmd') == 'PUT':
            self.commands.append(root)
            if not self.reject:
                jump = root.findtext('SERVER/List_Control/Jump_Line')
                if jump is not None and self.entries is not None:
                    listing = self.server_list.find('SERVER/List_Info')
                    index = int(jump)
                    start = ((index - 1) // 8) * 8 + 1
                    listing.find('Cursor_Position/Current_Line').text = str(index)
                    listing.find('Cursor_Position/Max_Line').text = str(len(self.entries))
                    current_list = listing.find('Current_List')
                    for offset in range(8):
                        entry = self.entries[start + offset - 1] if start + offset <= len(self.entries) else ('', 'Unselectable')
                        current_list.find(f'Line_{offset + 1}/Txt').text = entry[0]
                        current_list.find(f'Line_{offset + 1}/Attribute').text = entry[1]
                selection = root.findtext('SERVER/List_Control/Direct_Sel')
                if selection is not None:
                    current = int(self.server_list.findtext('SERVER/List_Info/Cursor_Position/Current_Line'))
                    FakeReceiver.selected_index = ((current - 1) // 8) * 8 + int(selection.split('_')[1])
                def update(source, destination):
                    for child in source:
                        target = destination.find(child.tag)
                        if target is not None:
                            if len(child):
                                update(child, target)
                            else:
                                target.text = child.text
                if root.find('Main_Zone') is not None:
                    update(root.find('Main_Zone'), self.state.find('Main_Zone/Basic_Status'))
                elif root.find('SERVER/Play_Control') is not None:
                    update(root.find('SERVER/Play_Control'), self.server_play.find('SERVER/Play_Info'))
                elif root.find('Tuner/Play_Control') is not None:
                    update(root.find('Tuner/Play_Control'), self.tuner_play.find('Tuner/Play_Info'))
            self.send_xml(b'<YAMAHA_AV rsp="PUT" RC="4"/>' if self.reject else b'<YAMAHA_AV rsp="PUT" RC="0"/>')
        else:
            if root.find('Tuner/Play_Info') is not None:
                return self.send_xml(ET.tostring(self.tuner_play))
            if root.find('Tuner/Play_Control/Preset/Preset_Sel_Item') is not None:
                return self.send_xml((FIXTURES / 'tuner-presets.xml').read_bytes())
            if root.find('SERVER/List_Info') is not None:
                if self.list_release is not None:
                    self.list_entered.set()
                    self.list_release.wait(5)
                return self.send_xml(ET.tostring(self.server_list))
            if root.find('SERVER/Play_Info') is not None:
                return self.send_xml(ET.tostring(self.server_play))
            name = 'inputs.xml' if root.find('.//Input_Sel_Item') is not None else 'scenes.xml' if root.find('.//Scene_Sel_Item') is not None else 'status.xml'
            self.send_xml(ET.tostring(self.state) if name == 'status.xml' else (FIXTURES / name).read_bytes())

    def log_message(self, *args):
        pass


class Integration(unittest.TestCase):
    def setUp(self):
        FakeReceiver.list_entered = FakeReceiver.list_release = None
        FakeReceiver.entries = FakeReceiver.selected_index = None
        FakeReceiver.state = ET.parse(FIXTURES / 'status.xml').getroot()
        FakeReceiver.server_list = ET.parse(FIXTURES / 'server-list_info.xml').getroot()
        FakeReceiver.server_play = ET.parse(FIXTURES / 'server-play_info.xml').getroot()
        FakeReceiver.tuner_play = ET.parse(FIXTURES / 'tuner-play.xml').getroot()

    @classmethod
    def setUpClass(cls):
        cls.fake = ThreadingHTTPServer(('127.0.0.1', 0), FakeReceiver)
        cls.data_dir = tempfile.TemporaryDirectory()
        Handler.receiver = Receiver(f'127.0.0.1:{cls.fake.server_port}', data_dir=cls.data_dir.name)
        cls.web = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        cls.url = f'http://127.0.0.1:{cls.web.server_port}'
        for server in (cls.fake, cls.web):
            threading.Thread(target=server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        for server in (cls.web, cls.fake):
            server.shutdown()
            server.server_close()
        cls.data_dir.cleanup()

    def test_rds_names_survive_restart_and_follow_frequency(self):
        with tempfile.TemporaryDirectory() as directory:
            receiver = Receiver(f'127.0.0.1:{self.fake.server_port}', data_dir=directory)
            receiver.tuner_play()
            self.assertFalse(receiver.names_file.exists())
            receiver.tuner_play()
            restarted = Receiver(receiver.host, data_dir=directory)
            presets = restarted.tuner_presets()
            self.assertEqual(presets[8]['label'], '9 : RADIO · FM 97.00')
            self.assertEqual(presets[0]['label'], '1 : FM 87.50 MHz')
            FakeReceiver.tuner_play.find('Tuner/Play_Info/Meta_Info/Program_Service').text = ''
            restarted.tuner_play()
            restarted.tuner_play()
            self.assertEqual(restarted.tuner_presets()[8]['label'], '9 : RADIO · FM 97.00')
            # A reused preset number at another frequency must not inherit the old name.
            presets_xml = ET.fromstring((FIXTURES / 'tuner-presets.xml').read_bytes())
            presets_xml.find('Tuner/Play_Control/Preset/Preset_Sel_Item/Item_10/Title').text = '9 : FM 99.00 MHz'
            original_xml = restarted.xml
            restarted.xml = lambda *args, **kwargs: presets_xml
            self.assertEqual(restarted.tuner_presets()[8]['label'], '9 : FM 99.00 MHz')
            restarted.xml = original_xml

    def request(self, path, body=None, headers=None):
        req = urllib.request.Request(self.url + path, json.dumps(body).encode() if body is not None else None,
                                     headers or {'Content-Type': 'application/json', 'X-Yamaha-Control': '1'})
        try:
            response = urllib.request.urlopen(req)
        except urllib.error.HTTPError as exc:
            response = exc
        with response:
            return response.status, response.read()

    def test_status_and_device_choices(self):
        code, raw = self.request('/api/config')
        config = json.loads(raw)
        self.assertEqual((code, config['model'], len(config['inputs']), len(config['scenes']), len(config['programs'])), (200, 'RX-V575', 19, 4, 19))
        self.assertNotIn('iPod (USB)', [item['value'] for item in config['inputs']])
        code, raw = self.request('/api/status')
        status = json.loads(raw)
        self.assertEqual((code, status['volume'], status['power'], status['input'], status['bass']), (200, -56, 'Standby', 'AV1', 6))
        self.assertTrue(status['straight'])

    def test_commands_and_validation(self):
        FakeReceiver.commands.clear()
        for action, value in [('volume', -42.5), ('mute', True), ('input', 'AV1'), ('scene', 'Scene 2'), ('program', '7ch Stereo'),
                              ('bass', -1.5), ('drc', True), ('power', 'On'), ('playback', 'Pause'), ('sleep', '30 min')]:
            self.assertEqual(self.request('/api/command', {'action': action, 'value': value})[0], 200)
        self.assertEqual(FakeReceiver.commands[0].findtext('Main_Zone/Volume/Lvl/Val'), '-425')
        self.assertEqual(FakeReceiver.commands[3].findtext('Main_Zone/Scene/Scene_Sel'), 'Scene 2')
        self.assertEqual(FakeReceiver.commands[6].findtext('Main_Zone/Sound_Video/Adaptive_DRC'), 'Auto')
        count = len(FakeReceiver.commands)
        for action, value in [('volume', 0), ('volume', -81), ('volume', -20.25), ('volume', True), ('volume', float('nan')),
                              ('mute', 'On'), ('input', '<bad>'), ('input', 'iPod (USB)'), ('scene', 'Scene 9'), ('unknown', 1)]:
            self.assertEqual(self.request('/api/command', {'action': action, 'value': value})[0], 400)
        self.assertEqual(len(FakeReceiver.commands), count)
        FakeReceiver.reject = True
        try:
            code, raw = self.request('/api/command', {'action': 'mute', 'value': False})
            self.assertEqual(code, 502)
            self.assertIn('RC=4', json.loads(raw)['error'])
        finally:
            FakeReceiver.reject = False

    def test_http_boundaries(self):
        command = {'action': 'power', 'value': 'On'}
        count = len(FakeReceiver.commands)
        self.assertEqual(self.request('/api/command', command, {'Content-Type': 'text/plain'})[0], 415)
        self.assertEqual(self.request('/api/command', command, {'Content-Type': 'application/json', 'X-Yamaha-Control': '1', 'Origin': 'https://other.example'})[0], 403)
        self.assertEqual(len(FakeReceiver.commands), count)
        self.assertEqual(self.request('/../server.py')[0], 404)
        self.assertEqual(self.request('/.env')[0], 404)
        self.assertEqual(self.request('/healthz')[0], 200)
        self.assertEqual(self.request('/')[0], 200)

    def test_settings_persistence_validation_and_hidden_inputs(self):
        original = Handler.receiver
        path = original.settings_file
        try:
            code, raw = self.request('/api/settings')
            self.assertEqual(code, 200)
            self.assertEqual(json.loads(raw)['host'], original.host)
            settings = {'host': original.host, 'visible_inputs': ['SERVER', 'TUNER']}
            self.assertEqual(self.request('/api/settings', settings)[0], 200)
            restarted = Receiver('192.168.1.1', data_dir=original.settings_file.parent)
            self.assertEqual(restarted.settings(), settings)
            config = json.loads(self.request('/api/config')[1])
            self.assertEqual(config['visible_inputs'], ['SERVER', 'TUNER'])
            self.assertEqual(len(config['inputs']), 19, 'Settings must retain every available source')
            before = path.read_bytes()
            for invalid in [{'host': 'http://192.168.1.132', 'visible_inputs': []},
                            {'host': '192.168.1.999', 'visible_inputs': []},
                            {'host': original.host, 'visible_inputs': ['SERVER', 'SERVER']},
                            {'host': original.host, 'visible_inputs': 'SERVER'},
                            {'host': original.host + '/path', 'visible_inputs': []}]:
                self.assertEqual(self.request('/api/settings', invalid)[0], 400)
            self.assertEqual(self.request('/api/settings', settings, {'Content-Type': 'application/json', 'X-Yamaha-Control': '1', 'Origin': 'https://other.example'})[0], 403)
            self.assertEqual(path.read_bytes(), before)
            self.assertEqual(self.request('/api/settings', {'host': original.host, 'visible_inputs': []})[0], 200)
            self.assertEqual(json.loads(self.request('/api/config')[1])['visible_inputs'], [])
        finally:
            Handler.receiver = original
            path.unlink(missing_ok=True)

    def test_tuner_state_and_commands(self):
        self.assertEqual(json.loads(self.request('/api/tuner/play')[1]), {'active': False})
        FakeReceiver.state.find('Main_Zone/Basic_Status/Power_Control/Power').text = 'On'
        FakeReceiver.state.find('Main_Zone/Basic_Status/Input/Input_Sel').text = 'TUNER'
        play = json.loads(self.request('/api/tuner/play')[1])['play']
        self.assertEqual((play['band'], play['frequency'], play['preset'], play['station']), ('FM', 97, '9', 'RADIO'))
        self.assertTrue(play['tuned'] and play['stereo'])
        presets = json.loads(self.request('/api/tuner/presets')[1])['presets']
        self.assertEqual(len(presets), 40)
        self.assertEqual(presets[0]['value'], '1')
        for action, value, path, expected in [
            ('tuner_frequency', {'band': 'FM', 'frequency': 97.05}, 'Tuner/Play_Control/Tuning/Freq/FM/Val', '9705'),
            ('tuner_search', 'Up', 'Tuner/Play_Control/Tuning/Freq/FM/Val', 'Auto Up'),
            ('tuner_search', 'Cancel', 'Tuner/Play_Control/Tuning/Freq/FM/Val', 'Cancel'),
            ('tuner_preset', '9', 'Tuner/Play_Control/Preset/Preset_Sel', '9'),
            ('tuner_preset', 'Down', 'Tuner/Play_Control/Preset/Preset_Sel', 'Down'),
            ('tuner_band', 'AM', 'Tuner/Play_Control/Tuning/Band', 'AM'),
            ('tuner_frequency', {'band': 'AM', 'frequency': 1080}, 'Tuner/Play_Control/Tuning/Freq/AM/Val', '1080')]:
            self.assertEqual(self.request('/api/command', {'action': action, 'value': value})[0], 200)
            self.assertEqual(FakeReceiver.commands[-1].findtext(path), expected)
            self.assertIsNone(FakeReceiver.commands[-1].find('Main_Zone'))
        count = len(FakeReceiver.commands)
        for action, value in [('tuner_frequency', {'band': 'FM', 'frequency': 97}),
                              ('tuner_frequency', {'band': 'AM', 'frequency': 1081}),
                              ('tuner_frequency', {'band': 'AM', 'frequency': True}),
                              ('tuner_frequency', {'band': 'AM', 'frequency': 1800}),
                              ('tuner_preset', 'No Preset'), ('tuner_preset', '41'),
                              ('tuner_search', 'Bad'), ('tuner_band', 'Bad')]:
            self.assertEqual(self.request('/api/command', {'action': action, 'value': value})[0], 400)
        FakeReceiver.tuner_play.find('Tuner/Play_Info/Tuning/Freq/Current/Val').text = 'Auto Up'
        self.assertIsNone(json.loads(self.request('/api/tuner/play')[1])['play']['frequency'])
        FakeReceiver.state.find('Main_Zone/Basic_Status/Input/Input_Sel').text = 'HDMI1'
        self.assertEqual(self.request('/api/command', {'action': 'tuner_band', 'value': 'FM'})[0], 400)
        self.assertEqual(len(FakeReceiver.commands), count)

    def test_server_browse_and_playback(self):
        self.assertEqual(json.loads(self.request('/api/server')[1]), {'active': False})
        FakeReceiver.state.find('Main_Zone/Basic_Status/Power_Control/Power').text = 'On'
        FakeReceiver.state.find('Main_Zone/Basic_Status/Input/Input_Sel').text = 'SERVER'
        FakeReceiver.server_list.find('SERVER/List_Info/Cursor_Position/Max_Line').text = '9'
        code, raw = self.request('/api/server')
        info = json.loads(raw)
        self.assertEqual(code, 200)
        self.assertEqual((info['list']['name'], info['list']['items'][0]['label'], info['play']['status']), ('Media Server', 'docker 10.11', 'Stop'))
        self.assertEqual(info['list']['items'][0]['kind'], 'Container')
        context = info['list']['context']
        commands = [('server_select', {'target': 1, 'context': context}, 'SERVER/List_Control/Direct_Sel', 'Line_1'),
                    ('server_page', {'target': 'Down', 'context': context}, 'SERVER/List_Control/Page', 'Down'),
                    ('server_cursor', {'target': 'Return to Home', 'context': context}, 'SERVER/List_Control/Cursor', 'Return to Home'),
                    ('server_repeat', 'All', 'SERVER/Play_Control/Play_Mode/Repeat', 'All'),
                    ('server_shuffle', True, 'SERVER/Play_Control/Play_Mode/Shuffle', 'On'),
                    ('server_playback', 'Pause', 'SERVER/Play_Control/Playback', 'Pause')]
        for action, value, path, expected in commands:
            self.assertEqual(self.request('/api/command', {'action': action, 'value': value})[0], 200)
            self.assertEqual(FakeReceiver.commands[-1].findtext(path), expected)
            self.assertIsNone(FakeReceiver.commands[-1].find('Main_Zone'))
        play = json.loads(self.request('/api/server')[1])['play']
        self.assertEqual(play['repeat'], 'All')
        self.assertTrue(play['shuffle'])
        count = len(FakeReceiver.commands)
        invalid = [('server_select', {'target': 2, 'context': context}), ('server_select', {'target': True, 'context': context}),
                   ('server_select', {'target': 1, 'context': 'stale'}), ('server_cursor', {'target': 'Return', 'context': context}),
                   ('server_page', {'target': 'Up', 'context': context}), ('server_repeat', 'Bad'), ('server_shuffle', 'On'),
                   ('server_playback', 'Bad'), ('server_select', 1), ('server_arbitrary', 'anything')]
        for action, value in invalid:
            self.assertEqual(self.request('/api/command', {'action': action, 'value': value})[0], 400)
        FakeReceiver.server_list.find('SERVER/List_Info/Current_List/Line_1/Txt').text = 'Changed'
        self.assertEqual(self.request('/api/command', {'action': 'server_select', 'value': {'target': 1, 'context': context}})[0], 400)
        FakeReceiver.server_list.find('SERVER/List_Info/Menu_Status').text = 'Busy'
        context = json.loads(self.request('/api/server')[1])['list']['context']
        self.assertEqual(self.request('/api/command', {'action': 'server_select', 'value': {'target': 1, 'context': context}})[0], 400)
        FakeReceiver.state.find('Main_Zone/Basic_Status/Input/Input_Sel').text = 'HDMI1'
        self.assertEqual(self.request('/api/command', {'action': 'server_repeat', 'value': 'Off'})[0], 400)
        self.assertEqual(len(FakeReceiver.commands), count)


    def test_continuous_list_and_selection_of_previously_loaded_item(self):
        FakeReceiver.state.find('Main_Zone/Basic_Status/Power_Control/Power').text = 'On'
        FakeReceiver.state.find('Main_Zone/Basic_Status/Input/Input_Sel').text = 'SERVER'
        FakeReceiver.entries = [(f'Track {index}', 'Item') for index in range(1, 21)]
        FakeReceiver.server_list.find('SERVER/List_Info/Cursor_Position/Max_Line').text = '20'
        initial = json.loads(self.request('/api/server/list')[1])['list']
        folder = initial['folder']
        for start in (1, 9, 17):
            self.assertEqual(self.request('/api/command', {'action': 'server_jump', 'value': {'target': start, 'folder': folder}})[0], 200)
            listing = json.loads(self.request('/api/server/list')[1])['list']
            self.assertEqual(listing['folder'], folder)
            self.assertEqual([item['index'] for item in listing['items']], list(range(start, min(start + 8, 21))))
        self.assertEqual(self.request('/api/command', {'action': 'server_item', 'value': {'target': 2, 'folder': folder, 'label': 'Track 2'}})[0], 200)
        self.assertEqual(FakeReceiver.selected_index, 2)
        self.assertEqual(FakeReceiver.commands[-2].findtext('SERVER/List_Control/Jump_Line'), '2')
        self.assertEqual(FakeReceiver.commands[-1].findtext('SERVER/List_Control/Direct_Sel'), 'Line_2')
        count = len(FakeReceiver.commands)
        for action, value in [('server_jump', {'target': 21, 'folder': folder}),
                              ('server_item', {'target': 2, 'folder': folder, 'label': 'Wrong track'}),
                              ('server_item', {'target': True, 'folder': folder, 'label': 'Track 1'}),
                              ('server_jump', {'target': 1, 'folder': 'stale'})]:
            self.assertEqual(self.request('/api/command', {'action': action, 'value': value})[0], 400)
        self.assertEqual(len(FakeReceiver.commands), count)

    def test_slow_list_does_not_block_status_player_or_commands(self):
        FakeReceiver.state.find('Main_Zone/Basic_Status/Power_Control/Power').text = 'On'
        FakeReceiver.state.find('Main_Zone/Basic_Status/Input/Input_Sel').text = 'SERVER'
        FakeReceiver.list_entered = threading.Event()
        FakeReceiver.list_release = threading.Event()
        with ThreadPoolExecutor(max_workers=4) as pool:
            listing = pool.submit(self.request, '/api/server/list')
            try:
                self.assertTrue(FakeReceiver.list_entered.wait(2), 'List request did not start')
                status = pool.submit(self.request, '/api/status')
                player = pool.submit(self.request, '/api/server/play')
                command = pool.submit(self.request, '/api/command', {'action': 'mute', 'value': True})
                self.assertEqual(status.result(timeout=1)[0], 200)
                code, raw = player.result(timeout=1)
                self.assertEqual(code, 200)
                self.assertIn('play', json.loads(raw))
                self.assertNotIn('list', json.loads(raw))
                self.assertEqual(command.result(timeout=1)[0], 200)
                self.assertFalse(listing.done(), 'The list should still be waiting')
            finally:
                FakeReceiver.list_release.set()
            code, raw = listing.result(timeout=2)
            self.assertEqual(code, 200)
            self.assertIn('list', json.loads(raw))
            self.assertNotIn('play', json.loads(raw))

    def test_click_after_background_cursor_moved_or_is_busy(self):
        FakeReceiver.state.find('Main_Zone/Basic_Status/Power_Control/Power').text = 'On'
        FakeReceiver.state.find('Main_Zone/Basic_Status/Input/Input_Sel').text = 'SERVER'
        FakeReceiver.entries = [(f'Track {index}', 'Item') for index in range(1, 21)]
        FakeReceiver.server_list.find('SERVER/List_Info/Cursor_Position/Max_Line').text = '20'
        initial = json.loads(self.request('/api/server/list')[1])['list']
        folder = initial['folder']
        self.assertEqual(self.request('/api/command', {'action': 'server_jump', 'value': {'target': 9, 'folder': folder}})[0], 200)
        # Folder navigation uses its identity rather than the page changed by preloading.
        self.assertEqual(self.request('/api/command', {'action': 'server_cursor', 'value': {'target': 'Return to Home', 'folder': folder}})[0], 200)
        status = FakeReceiver.server_list.find('SERVER/List_Info/Menu_Status')
        status.text = 'Busy'
        timer = threading.Timer(0.2, lambda: setattr(status, 'text', 'Ready'))
        timer.start()
        try:
            code, raw = self.request('/api/command', {'action': 'server_item', 'value': {'target': 2, 'folder': folder, 'label': 'Track 2'}})
            self.assertEqual(code, 200, raw)
            self.assertEqual(FakeReceiver.selected_index, 2)
        finally:
            timer.join()


if __name__ == '__main__':
    unittest.main()
