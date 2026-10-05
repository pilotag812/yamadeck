"""Local Yamaha XML controller. Python standard library only."""
import json
import hashlib
import ipaddress
import math
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).parent


class ReceiverError(Exception):
    pass


def envelope(command, path, value):
    root = ET.Element('YAMAHA_AV', cmd=command)
    node = root
    for tag in path.split('/'):
        node = ET.SubElement(node, tag)
    if isinstance(value, dict):
        for tag, text in value.items():
            ET.SubElement(node, tag).text = str(text)
    else:
        node.text = str(value)
    return ET.tostring(root, encoding='utf-8')


def parse_xml(data):
    if b'<!DOCTYPE' in data.upper() or b'<!ENTITY' in data.upper():
        raise ReceiverError('Некорректный XML ресивера')
    try:
        root = ET.fromstring(data)
    except ET.ParseError as exc:
        raise ReceiverError('Ресивер вернул некорректный XML') from exc
    return root


class Receiver:
    def __init__(self, host, max_volume=-10, data_dir=None, load_settings=True):
        directory = Path(data_dir or os.environ.get('DATA_DIR', ROOT / 'data'))
        self.settings_file = directory / 'settings.json'
        settings = {}
        if load_settings:
            try:
                settings = json.loads(self.settings_file.read_text(encoding='utf-8'))
            except FileNotFoundError:
                pass
        host = settings.get('host', host)
        self.visible_inputs = settings.get('visible_inputs')
        parsed = urllib.parse.urlsplit('http://' + host)
        if not parsed.hostname or parsed.netloc != host or parsed.username or parsed.password or parsed.path or parsed.query or parsed.fragment:
            raise ValueError('RECEIVER_HOST должен содержать только IP/имя и необязательный порт')
        self.base = 'http://' + host
        self.host = host
        self.max_volume = float(max_volume)
        if not math.isfinite(self.max_volume) or not -80.5 <= self.max_volume <= 16.5 or self.max_volume * 2 != round(self.max_volume * 2):
            raise ValueError('MAX_VOLUME_DB: от -80.5 до 16.5 с шагом 0.5')
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        self.lock = threading.Lock()
        self.info = None
        self.names_lock = threading.Lock()
        self.rds_candidate = None
        self.names_file = directory / 'radio-names.json'
        try:
            saved = json.loads(self.names_file.read_text(encoding='utf-8'))
            self.radio_names = {key: name for key, name in saved.items() if isinstance(key, str) and isinstance(name, str)}
        except FileNotFoundError:
            self.radio_names = {}

    def request(self, path, data=None):
        req = urllib.request.Request(self.base + path, data, {'Content-Type': 'text/xml; charset=utf-8'})
        try:
            with self.opener.open(req, timeout=4) as response:
                body = response.read(2_000_001)
            if len(body) > 2_000_000:
                raise ReceiverError('Слишком большой ответ ресивера')
            return parse_xml(body)
        except (urllib.error.URLError, OSError) as exc:
            raise ReceiverError(f'Нет связи с ресивером {self.host}. Проверьте сеть и Network Standby.') from exc

    def xml(self, command, path, value='GetParam', zone='Main_Zone'):
        root = self.request('/YamahaRemoteControl/ctrl', envelope(command, zone + '/' + path, value))
        if root.tag != 'YAMAHA_AV' or root.get('RC') != '0':
            raise ReceiverError(f'Ресивер отклонил команду (RC={root.get("RC", "?")})')
        return root

    def config(self):
        if self.info is None:
            desc = self.request('/YamahaRemoteControl/desc.xml')
            def items(path):
                node = self.xml('GET', path).find('Main_Zone/' + path)
                if node is None:
                    raise ReceiverError('Нет списка входов/сцен в ответе ресивера')
                return [{'value': item.findtext('Param'), 'label': (item.findtext('Title') or item.findtext('Param') or '').strip()}
                        for item in node if 'W' in (item.findtext('RW') or '') and item.findtext('Param')]
            main = next((m for m in desc.iter('Menu') if m.get('YNC_Tag') == 'Main_Zone' and m.find('Cmd_List/Define[@ID="P9"]') is not None), None)
            programs = [] if main is None else [n.text for put in main.iter('Put_2')
                                                if put.find('Cmd[@ID="P9"]') is not None
                                                for n in put.findall('Param_1/Direct')]
            self.info = {'model': desc.get('Unit_Name', 'Yamaha'), 'host': self.host,
                         'inputs': items('Input/Input_Sel_Item'), 'scenes': items('Scene/Scene_Sel_Item'),
                         'programs': programs, 'min_volume': -80.5, 'max_volume': self.max_volume}
        return {**self.info, 'visible_inputs': self.visible_inputs}

    def settings(self):
        return {'host': self.host, 'visible_inputs': self.visible_inputs}

    def save_settings(self, data):
        if not isinstance(data, dict) or set(data) != {'host', 'visible_inputs'} or not isinstance(data['host'], str):
            raise ValueError('Требуются IP ресивера и список источников')
        host, visible = data['host'].strip(), data['visible_inputs']
        parsed = urllib.parse.urlsplit('http://' + host)
        try:
            ipaddress.ip_address(parsed.hostname or '')
            port = parsed.port
        except ValueError as exc:
            raise ValueError('Укажите корректный IP адрес ресивера') from exc
        if port is not None and not 1 <= port <= 65535:
            raise ValueError('Некорректный порт')
        if visible is not None and (not isinstance(visible, list) or len(visible) > 64 or any(not isinstance(value, str) or not value or len(value) > 64 for value in visible) or len(set(visible)) != len(visible)):
            raise ValueError('Некорректный список источников')
        replacement = Receiver(host, self.max_volume, self.settings_file.parent, load_settings=False)
        replacement.visible_inputs = visible
        try:
            self.settings_file.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.settings_file.with_suffix('.tmp')
            temporary.write_text(json.dumps(replacement.settings(), ensure_ascii=False, indent=2), encoding='utf-8')
            temporary.replace(self.settings_file)
        except OSError as exc:
            raise ReceiverError('Не удалось сохранить настройки. Проверьте каталог данных.') from exc
        return replacement

    def status(self):
        node = self.xml('GET', 'Basic_Status').find('Main_Zone/Basic_Status')
        if node is None:
            raise ReceiverError('Нет состояния в ответе ресивера')
        def text(path, default=''):
            return node.findtext(path, default)
        def level(path):
            try:
                return int(text(path + '/Val')) / 10 ** int(text(path + '/Exp'))
            except (ValueError, ZeroDivisionError, OverflowError) as exc:
                raise ReceiverError('Некорректный уровень громкости/тембра') from exc
        return {'power': text('Power_Control/Power'), 'sleep': text('Power_Control/Sleep'),
                'volume': level('Volume/Lvl'), 'mute': text('Volume/Mute') == 'On',
                'input': text('Input/Input_Sel'), 'input_label': text('Input/Input_Sel_Item_Info/Title').strip(),
                'program': text('Surround/Program_Sel/Current/Sound_Program'),
                'straight': text('Surround/Program_Sel/Current/Straight') == 'On',
                'enhancer': text('Surround/Program_Sel/Current/Enhancer') == 'On',
                'direct': text('Sound_Video/Direct/Mode') == 'On',
                'drc': text('Sound_Video/Adaptive_DRC') == 'Auto',
                'bass': level('Sound_Video/Tone/Bass'), 'treble': level('Sound_Video/Tone/Treble')}

    def server_list(self):
        node = self.xml('GET', 'List_Info', zone='SERVER').find('SERVER/List_Info')
        if node is None:
            raise ReceiverError('Нет списка медиасервера в ответе ресивера')
        try:
            current = int(node.findtext('Cursor_Position/Current_Line', '1'))
            total = int(node.findtext('Cursor_Position/Max_Line', '0'))
            layer = int(node.findtext('Menu_Layer', '1'))
        except ValueError as exc:
            raise ReceiverError('Некорректная позиция в списке медиасервера') from exc
        start = max(0, (current - 1) // 8) * 8 + 1
        items = []
        for line in range(1, 9):
            label = node.findtext(f'Current_List/Line_{line}/Txt', '')
            kind = node.findtext(f'Current_List/Line_{line}/Attribute', 'Unselectable')
            if label:
                items.append({'line': line, 'index': start + line - 1, 'label': label, 'kind': kind,
                              'selectable': kind in ('Container', 'Item')})
        result = {'ready': node.findtext('Menu_Status') == 'Ready', 'layer': layer,
                  'name': node.findtext('Menu_Name', 'Медиасервер'), 'start': start, 'total': total, 'items': items}
        result['folder'] = hashlib.sha256(json.dumps({key: result[key] for key in ('layer', 'name', 'total')}, sort_keys=True).encode()).hexdigest()
        # Bind a click to the displayed page, so a remote cursor cannot make it play another song.
        result['context'] = hashlib.sha256(json.dumps(result, sort_keys=True).encode()).hexdigest()
        return result

    def server_info(self, part=None):
        state = self.status()
        if state['input'] != 'SERVER' or state['power'] != 'On':
            return {'active': False}
        result = {'active': True}
        if part != 'play':
            result['list'] = self.server_list()
        if part == 'list':
            return result
        node = self.xml('GET', 'Play_Info', zone='SERVER').find('SERVER/Play_Info')
        if node is None:
            raise ReceiverError('Нет состояния воспроизведения SERVER')
        result['play'] = {
            'available': node.findtext('Feature_Availability') == 'Ready',
            'status': node.findtext('Playback_Info', 'Stop'),
            'song': node.findtext('Meta_Info/Song', ''), 'artist': node.findtext('Meta_Info/Artist', ''),
            'album': node.findtext('Meta_Info/Album', ''),
            'repeat': node.findtext('Play_Mode/Repeat', 'Off'), 'shuffle': node.findtext('Play_Mode/Shuffle') == 'On'}
        return result

    def server_command(self, action, value):
        state = self.status()
        if state['input'] != 'SERVER' or state['power'] != 'On':
            raise ValueError('Для управления медиасервером включите ресивер и выберите SERVER')
        if action in ('server_jump', 'server_item'):
            keys = {'target', 'folder'} if action == 'server_jump' else {'target', 'folder', 'label'}
            if not isinstance(value, dict) or set(value) != keys or type(value['target']) is not int:
                raise ValueError('Некорректный пункт списка')
            listing = self.ready_server_list() if action == 'server_item' else self.server_list()
            target = value['target']
            if not listing['ready'] or listing['folder'] != value['folder'] or not 1 <= target <= min(listing['total'], 65536):
                raise ValueError('Папка изменилась или ещё загружается. Дождитесь обновления.')
            if action == 'server_jump':
                return self.xml('PUT', 'List_Control/Jump_Line', target, zone='SERVER')
            if not listing['start'] <= target < listing['start'] + 8:
                self.xml('PUT', 'List_Control/Jump_Line', target, zone='SERVER')
                deadline = time.monotonic() + 3
                while True:
                    listing = self.server_list()
                    if listing['ready'] and listing['start'] <= target < listing['start'] + 8:
                        break
                    if time.monotonic() >= deadline:
                        raise ReceiverError('Ресивер ещё загружает список. Выберите пункт повторно.')
                    time.sleep(0.1)
            if listing['folder'] != value['folder']:
                raise ValueError('Папка изменилась. Обновите список.')
            item = next((item for item in listing['items'] if item['index'] == target), None)
            if item is None or not item['selectable'] or item['label'] != value['label']:
                raise ValueError('Пункт списка изменился или недоступен. Обновите список.')
            path, value = 'List_Control/Direct_Sel', f'Line_{item["line"]}'
        elif action in ('server_select', 'server_page', 'server_cursor'):
            folder_navigation = action == 'server_cursor' and isinstance(value, dict) and set(value) == {'target', 'folder'}
            if not folder_navigation and (not isinstance(value, dict) or set(value) != {'target', 'context'}):
                raise ValueError('Требуются target и context списка')
            listing = self.ready_server_list() if folder_navigation else self.server_list()
            key = 'folder' if folder_navigation else 'context'
            if not listing['ready'] or value[key] != listing[key]:
                raise ValueError('Список изменился или ещё загружается. Дождитесь обновления и выберите снова.')
            target = value['target']
            if action == 'server_select':
                if type(target) is not int or not any(item['line'] == target and item['selectable'] for item in listing['items']):
                    raise ValueError('Этот пункт списка недоступен')
                path, value = 'List_Control/Direct_Sel', f'Line_{target}'
            elif action == 'server_page':
                if target not in ('Up', 'Down') or (target == 'Up' and listing['start'] <= 1) or (target == 'Down' and listing['start'] + 8 > listing['total']):
                    raise ValueError('Страница недоступна')
                path, value = 'List_Control/Page', target
            else:
                if target not in ('Return', 'Return to Home') or (target == 'Return' and listing['layer'] <= 1):
                    raise ValueError('Некорректная навигация по папкам')
                path, value = 'List_Control/Cursor', target
        elif action == 'server_repeat':
            if value not in ('Off', 'One', 'All'):
                raise ValueError('Некорректный режим повтора')
            path = 'Play_Control/Play_Mode/Repeat'
        elif action == 'server_shuffle':
            if type(value) is not bool:
                raise ValueError('Требуется логическое значение')
            path, value = 'Play_Control/Play_Mode/Shuffle', 'On' if value else 'Off'
        elif action == 'server_playback':
            if value not in ('Play', 'Pause', 'Stop', 'Skip Fwd', 'Skip Rev'):
                raise ValueError('Некорректная команда воспроизведения')
            path = 'Play_Control/Playback'
        else:
            raise ValueError('Неизвестная команда SERVER')
        self.xml('PUT', path, value, zone='SERVER')

    def ready_server_list(self):
        deadline = time.monotonic() + 3
        while True:
            listing = self.server_list()
            if listing['ready']:
                return listing
            if time.monotonic() >= deadline:
                raise ReceiverError('Ресивер ещё загружает список. Выберите пункт повторно.')
            time.sleep(0.1)

    def command(self, action, value):
        if action.startswith('tuner_'):
            return self.tuner_command(action, value)
        if action.startswith('server_'):
            return self.server_command(action, value)
        toggles = {'mute': 'Volume/Mute', 'straight': 'Surround/Program_Sel/Current/Straight',
                   'enhancer': 'Surround/Program_Sel/Current/Enhancer', 'direct': 'Sound_Video/Direct/Mode',
                   'drc': 'Sound_Video/Adaptive_DRC'}
        if action in toggles:
            if type(value) is not bool:
                raise ValueError('Требуется логическое значение')
            path, value = toggles[action], ('Auto' if action == 'drc' else 'On') if value else 'Off'
        elif action == 'power':
            if value not in ('On', 'Standby'):
                raise ValueError('Некорректное питание')
            path = 'Power_Control/Power'
        elif action in ('volume', 'bass', 'treble'):
            low, high = (-80.5, self.max_volume) if action == 'volume' else (-6, 6)
            if type(value) not in (int, float) or not low <= value <= high or not math.isfinite(value) or value * 2 != round(value * 2):
                raise ValueError(f'Допустимо от {low} до {high} dB с шагом 0.5')
            path = 'Volume/Lvl' if action == 'volume' else 'Sound_Video/Tone/' + action.title()
            value = {'Val': round(value * 10), 'Exp': 1, 'Unit': 'dB'}
        elif action in ('input', 'scene', 'program'):
            config = self.config()
            choices = config['programs'] if action == 'program' else [item['value'] for item in config[action + 's']]
            if value not in choices:
                raise ValueError('Неподдерживаемый вход, сцена или режим звука')
            path = {'input': 'Input/Input_Sel', 'scene': 'Scene/Scene_Sel', 'program': 'Surround/Program_Sel/Current/Sound_Program'}[action]
        elif action == 'sleep':
            if value not in ('Off', '30 min', '60 min', '90 min', '120 min'):
                raise ValueError('Некорректный таймер')
            path = 'Power_Control/Sleep'
        elif action == 'playback':
            if value not in ('Play', 'Pause', 'Stop', 'Skip Fwd', 'Skip Rev'):
                raise ValueError('Некорректная команда воспроизведения')
            path = 'Play_Control/Playback'
        else:
            raise ValueError('Неизвестная команда')
        self.xml('PUT', path, value)

    def tuner_play(self):
        node = self.xml('GET', 'Play_Info', zone='Tuner').find('Tuner/Play_Info')
        if node is None:
            raise ReceiverError('Нет состояния тюнера в ответе ресивера')
        try:
            frequency = int(node.findtext('Tuning/Freq/Current/Val', '')) / 10 ** int(node.findtext('Tuning/Freq/Current/Exp', '0'))
        except (ValueError, OverflowError, ZeroDivisionError):
            frequency = None  # During auto search the receiver can return a text value.
        result = {'available': node.findtext('Feature_Availability') == 'Ready',
                'band': node.findtext('Tuning/Band', 'FM'), 'frequency': frequency,
                'preset': node.findtext('Preset/Preset_Sel', ''),
                'tuned': node.findtext('Signal_Info/Tuned') == 'Assert',
                'stereo': node.findtext('Signal_Info/Stereo') == 'Assert',
                'station': node.findtext('Meta_Info/Program_Service', '').strip(),
                'text': (node.findtext('Meta_Info/Radio_Text_A', '') or node.findtext('Meta_Info/Radio_Text_B', '')).strip(),
                'type': node.findtext('Meta_Info/Program_Type', '').strip()}
        self.remember_radio_name(result)
        return result

    def radio_key(self, band, frequency):
        return f'{self.host}/{band}/{round(frequency * 100)}'

    def remember_radio_name(self, play):
        # Two matching observations prevent a lingering RDS name being saved immediately after tuning.
        candidate = (self.radio_key(play['band'], play['frequency']), play['station']) if play['available'] and play['tuned'] and play['band'] == 'FM' and play['frequency'] is not None and play['station'] else None
        with self.names_lock:
            previous, self.rds_candidate = self.rds_candidate, candidate
            if candidate is None or candidate != previous or self.radio_names.get(candidate[0]) == candidate[1]:
                return
            names = {**self.radio_names, candidate[0]: candidate[1]}
            try:
                self.names_file.parent.mkdir(parents=True, exist_ok=True)
                temporary = self.names_file.with_suffix('.tmp')
                temporary.write_text(json.dumps(names, ensure_ascii=False, indent=2), encoding='utf-8')
                temporary.replace(self.names_file)
            except OSError as exc:
                raise ReceiverError('Не удалось сохранить название RDS. Проверьте каталог данных.') from exc
            self.radio_names = names

    def tuner_presets(self):
        node = self.xml('GET', 'Play_Control/Preset/Preset_Sel_Item', zone='Tuner').find('Tuner/Play_Control/Preset/Preset_Sel_Item')
        if node is None:
            raise ReceiverError('Нет списка радиостанций в ответе ресивера')
        items = [{'value': item.findtext('Param'), 'label': (item.findtext('Title') or '').strip()}
                 for item in node if 'W' in (item.findtext('RW') or '') and item.findtext('Param')]
        with self.names_lock:
            for item in items:
                match = re.search(r'\b(FM|AM)\s+(\d+(?:\.\d+)?)\s+(?:MHz|kHz)\b', item['label'])
                name = self.radio_names.get(self.radio_key(match[1], float(match[2]))) if match else None
                if name:
                    item['label'] = f'{item["value"]} : {name} · {match[1]} {match[2]}'
        return items

    def tuner_info(self, part):
        state = self.status()
        if state['input'] != 'TUNER' or state['power'] != 'On':
            return {'active': False}
        return {'active': True, part: self.tuner_play() if part == 'play' else self.tuner_presets()}

    def tuner_command(self, action, value):
        state = self.status()
        if state['input'] != 'TUNER' or state['power'] != 'On':
            raise ValueError('Включите ресивер и выберите TUNER')
        if action == 'tuner_band':
            if value not in ('FM', 'AM'):
                raise ValueError('Выберите FM или AM')
            path = 'Play_Control/Tuning/Band'
        elif action == 'tuner_preset':
            if not isinstance(value, str) or value not in ['Up', 'Down'] + [item['value'] for item in self.tuner_presets()]:
                raise ValueError('Сохранённая станция недоступна')
            path = 'Play_Control/Preset/Preset_Sel'
        elif action == 'tuner_frequency':
            if not isinstance(value, dict) or set(value) != {'band', 'frequency'} or value['band'] not in ('FM', 'AM'):
                raise ValueError('Требуются диапазон и частота')
            band, frequency = value['band'], value['frequency']
            low, high, step, scale = (8750, 10800, 5, 100) if band == 'FM' else (531, 1611, 9, 1)
            if type(frequency) not in (int, float) or not math.isfinite(frequency):
                raise ValueError('Некорректная частота')
            integer = round(frequency * scale)
            if not math.isclose(frequency * scale, integer, abs_tol=0.000001, rel_tol=0) or not low <= integer <= high or (integer - low) % step:
                raise ValueError('FM: 87.50–108.00 MHz, шаг 0.05; AM: 531–1611 kHz, шаг 9')
            if self.tuner_play()['band'] != band:
                raise ValueError('Диапазон изменился. Дождитесь обновления тюнера.')
            path = 'Play_Control/Tuning/Freq/' + band
            value = {'Val': integer, 'Exp': 2 if band == 'FM' else 0, 'Unit': 'MHz' if band == 'FM' else 'kHz'}
        elif action == 'tuner_search':
            if value not in ('Up', 'Down', 'Cancel'):
                raise ValueError('Некорректное направление поиска')
            band = self.tuner_play()['band']
            if band not in ('FM', 'AM'):
                raise ReceiverError('Неизвестный диапазон тюнера')
            path = 'Play_Control/Tuning/Freq/' + band + '/Val'
            value = 'Cancel' if value == 'Cancel' else 'Auto ' + value
        else:
            raise ValueError('Неизвестная команда тюнера')
        self.xml('PUT', path, value, zone='Tuner')
        with self.names_lock:
            self.rds_candidate = None


class Handler(BaseHTTPRequestHandler):
    receiver = None
    mutation_lock = threading.Lock()

    def setup(self):
        super().setup()
        self.connection.settimeout(10)

    def reply(self, status, body, content_type='application/json; charset=utf-8'):
        if not isinstance(body, bytes):
            body = json.dumps(body, ensure_ascii=False, allow_nan=False).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src 'self'; frame-ancestors 'none'; base-uri 'none'")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = urllib.parse.urlsplit(self.path).path
        if path == '/healthz':
            return self.reply(200, {'ok': True})
        files = {'/': ('index.html', 'text/html; charset=utf-8'), '/app.js': ('app.js', 'text/javascript; charset=utf-8'),
                 '/style.css': ('style.css', 'text/css; charset=utf-8')}
        if path in files:
            name, content_type = files[path]
            return self.reply(200, (ROOT / 'static' / name).read_bytes(), content_type)
        try:
            # Reads are independent; only mutations need the receiver lock.
            if path == '/api/config':
                return self.reply(200, self.receiver.config())
            if path == '/api/settings':
                return self.reply(200, self.receiver.settings())
            if path == '/api/status':
                return self.reply(200, self.receiver.status())
            if path in ('/api/tuner/play', '/api/tuner/presets'):
                return self.reply(200, self.receiver.tuner_info(path.rsplit('/', 1)[-1]))
            if path in ('/api/server', '/api/server/list', '/api/server/play'):
                part = path.rsplit('/', 1)[-1] if path != '/api/server' else None
                return self.reply(200, self.receiver.server_info(part))
            self.reply(404, {'error': 'Не найдено'})
        except ReceiverError as exc:
            self.reply(502, {'error': str(exc)})

    def do_POST(self):
        if self.path not in ('/api/command', '/api/settings'):
            return self.reply(404, {'error': 'Не найдено'})
        try:
            size = int(self.headers.get('Content-Length', '0'))
        except ValueError:
            return self.reply(400, {'error': 'Некорректный Content-Length'})
        if not 0 < size <= 4096:
            return self.reply(413, {'error': 'Некорректный размер запроса'})
        raw = self.rfile.read(size)
        # JSON + a custom header prevents other websites from issuing receiver commands.
        origin = self.headers.get('Origin')
        if (origin and urllib.parse.urlsplit(origin).netloc != self.headers.get('Host')) or self.headers.get('Sec-Fetch-Site') == 'cross-site':
            return self.reply(403, {'error': 'Недопустимый источник запроса'})
        if self.headers.get('X-Yamaha-Control') != '1' or self.headers.get_content_type() != 'application/json':
            return self.reply(415, {'error': 'Требуется JSON и заголовок X-Yamaha-Control: 1'})
        try:
            data = json.loads(raw)
            if self.path == '/api/settings':
                with self.mutation_lock:
                    Handler.receiver = self.receiver.save_settings(data)
                return self.reply(200, self.receiver.settings())
            if not isinstance(data, dict) or set(data) != {'action', 'value'} or not isinstance(data['action'], str):
                raise ValueError('Требуются action и value')
            with self.mutation_lock:
                self.receiver.command(data['action'], data['value'])
            self.reply(200, {'ok': True})
        except (ValueError, TypeError, UnicodeDecodeError) as exc:
            self.reply(400, {'error': str(exc)})
        except ReceiverError as exc:
            self.reply(502, {'error': str(exc)})


if __name__ == '__main__':
    Handler.receiver = Receiver(os.environ.get('RECEIVER_HOST', '192.168.1.132'), os.environ.get('MAX_VOLUME_DB', '-10'))
    server = ThreadingHTTPServer(('0.0.0.0', int(os.environ.get('PORT', '8080'))), Handler)
    print(f'Yamaha Web: port {server.server_port}, receiver {Handler.receiver.host}', flush=True)
    server.serve_forever()
