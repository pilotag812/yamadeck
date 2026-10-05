const $ = id => document.getElementById(id);
let state = null, config = null, online = false, configLoading = false, statusRequest = null;
let serverData = {list: null, play: null}, serverSource = '', serverRetry = null, listChanging = false;
const pending = new Set(), serverRequests = {list: null, play: null}, serverErrors = {list: '', play: ''};
const serverItems = new Map();
let serverFolder = '';
let serverPreload = null, serverPreloadTimer = null;
const tunerData = {play: null, presets: null}, tunerRequests = {play: null, presets: null}, tunerErrors = {play: '', presets: ''};
const playbackInputs = ['SERVER', 'NET RADIO', 'USB', 'iPod (USB)', 'AirPlay', 'Spotify'];
document.querySelectorAll('[data-control], [data-server-control], [data-tuner-control]').forEach(el => el.disabled = true);

async function api(path, body, signal) {
  const response = await fetch(path, {
    ...(body ? {method: 'POST', headers: {'Content-Type': 'application/json', 'X-Yamaha-Control': '1'}, body: JSON.stringify(body)} : {}),
    signal: signal ? AbortSignal.any([signal, AbortSignal.timeout(15000)]) : AbortSignal.timeout(15000)
  });
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || 'Не удалось выполнить запрос');
  return data;
}
function error(message = '') {
  $('error').hidden = !message;
  $('error').textContent = message;
}
function format(value) {
  return Number.isFinite(value) ? value.toLocaleString('ru-RU', {maximumFractionDigits: 1}) : '—';
}
function commandGroup(action) {
  if (action.startsWith('tuner_')) return 'tuner';
  if (['power', 'input', 'scene'].includes(action)) return 'source';
  if (['volume', 'quieter', 'louder'].includes(action)) return 'volume';
  if (['server_item', 'server_jump', 'server_cursor'].includes(action)) return 'server_list';
  if (['server_playback', 'server_repeat', 'server_shuffle'].includes(action)) return 'server_play';
  return action;
}
function controls() {
  document.querySelectorAll('[data-control]').forEach(el => {
    const action = el.dataset.action || (el.dataset.playback ? 'playback' : el.id);
    el.disabled = !online || !state || pending.has(commandGroup(action));
  });
  serverControls();
  tunerControls();
  if (!state || !online) return;
  const standby = state.power !== 'On';
  document.querySelectorAll('[data-playback]').forEach(el => el.disabled ||= standby || !playbackInputs.includes(state.input));
  $('program').disabled ||= state.straight || state.direct || !config?.programs.length;
  $('bass').disabled ||= state.direct;
  $('treble').disabled ||= state.direct;
  $('volume').disabled ||= !config;
  $('louder').disabled ||= !config || state.volume >= config.max_volume;
  $('quieter').disabled ||= !config || state.volume <= config.min_volume;
  document.querySelectorAll('[data-volume-preset]').forEach(button => {
    const value = Number(button.dataset.volumePreset);
    button.disabled ||= !config || value < config.min_volume || value > config.max_volume;
    button.setAttribute('aria-pressed', String(state.volume === value));
  });
}
function render() {
  if (!state) return;
  $('power-state').textContent = state.power === 'On' ? 'РЕСИВЕР ВКЛЮЧЁН' : 'РЕЖИМ ОЖИДАНИЯ';
  $('power-label').textContent = state.power === 'On' ? 'Выключить ресивер' : 'Включить ресивер';
  $('power').classList.toggle('is-on', state.power === 'On');
  $('volume-value').textContent = format(state.volume);
  $('current-input').textContent = state.input_label || state.input;
  for (const id of ['volume', 'bass', 'treble']) {
    if (document.activeElement !== $(id)) $(id).value = state[id];
    if (id !== 'volume') $(id + '-value').textContent = format(state[id]) + ' dB';
  }
  // The meter follows actual volume even if an external remote exceeds the web limit.
  const angle = Math.max(0, Math.min(270, (state.volume + 80.5) / ((config?.max_volume ?? -10) + 80.5 || 1) * 270));
  document.querySelector('.dial').style.background = `conic-gradient(from 225deg, var(--accent) 0deg, var(--accent) ${angle}deg, #353d2c ${angle}deg, #353d2c 270deg, transparent 270deg)`;
  for (const id of ['mute', 'straight', 'enhancer', 'direct', 'drc']) $(id).setAttribute('aria-pressed', String(state[id]));
  $('mute').textContent = state.mute ? 'Звук выключен' : 'Без звука';
  $('program').value = state.program;
  $('sleep').value = state.sleep;
  document.querySelectorAll('[data-input]').forEach(button => button.setAttribute('aria-pressed', String(button.dataset.input === state.input)));
  $('sound-hint').textContent = state.direct ? 'Direct включён: тембр и DSP обходятся.' : state.straight ? 'Straight включён: звук без DSP-обработки.' : 'Выберите программу обработки звука.';
  const source = state.input + '/' + state.power;
  if (source !== serverSource) {
    serverSource = source;
    for (const part of ['play', 'presets']) {
      tunerRequests[part]?.abort();
      tunerRequests[part] = null;
      tunerData[part] = null;
      tunerErrors[part] = '';
    }
    delete $('tuner-preset').dataset.signature;
    $('tuner-preset').replaceChildren(new Option('Загрузка…', ''));
    renderTuner();
    for (const part of ['list', 'play']) {
      serverRequests[part]?.abort();
      serverRequests[part] = null;
      serverData[part] = null;
      serverErrors[part] = '';
    }
    clearTimeout(serverRetry);
    cancelServerPreload();
    listChanging = false;
    resetServerBrowser();
    $('server-song').textContent = 'Нет выбранного трека';
    $('server-meta').textContent = '';
    $('server-play-state').textContent = 'ОСТАНОВЛЕНО';
    $('server-message').textContent = state.power === 'On' ? 'Загрузка списка…' : 'Включите ресивер для доступа к медиасерверу.';
  }
  $('server-panel').hidden = state.input !== 'SERVER';
  $('tuner-panel').hidden = state.input !== 'TUNER';
  $('source-panel').hidden = ['SERVER', 'TUNER'].includes(state.input);
  $('source-name').textContent = state.input_label || state.input;
  $('source-message').textContent = playbackInputs.includes(state.input) ? 'Управление воспроизведением выбранного источника.' : 'Этот вход получает звук от внешнего устройства. Воспроизведением управляйте на нём.';
  $('generic-playback').hidden = ['SERVER', 'TUNER'].includes(state.input);
  controls();
}

function serverControls() {
  const available = online && state?.input === 'SERVER' && state.power === 'On' && !pending.has('source');
  document.querySelectorAll('[data-server-control]').forEach(el => el.disabled = !available);
  if (!available) return;
  const listing = serverData.list;
  const listBlocked = !listing?.ready || listChanging || !!serverErrors.list || pending.has('server_list');
  $('server-back').disabled = listBlocked || listing.layer <= 1;
  $('server-home').disabled = listBlocked;
  document.querySelectorAll('[data-server-line]').forEach(el => el.disabled = listBlocked || el.dataset.selectable !== 'true');
  document.querySelectorAll('[data-server-playback], #server-repeat, #server-shuffle').forEach(el => el.disabled = !serverData.play?.available || !!serverErrors.play || pending.has('server_play'));
}

function renderServer() {
  const {list, play} = serverData;
  if (play) {
    $('server-song').textContent = play.song || 'Нет выбранного трека';
    $('server-play-state').textContent = {Play: 'ИГРАЕТ', Pause: 'ПАУЗА', Stop: 'ОСТАНОВЛЕНО'}[play.status] || play.status;
    $('server-repeat').value = play.repeat;
    $('server-shuffle').setAttribute('aria-pressed', String(play.shuffle));
  }
  $('server-meta').textContent = serverErrors.play || (play ? [play.artist, play.album].filter(Boolean).join(' · ') : 'Загрузка сведений о треке…');
  $('server-message').textContent = serverErrors.list || (listChanging || !list || !list.ready ? 'Ресивер загружает список…' : !list.total ? 'Список пуст. Проверьте, что DLNA-сервер доступен в домашней сети.' : 'Выберите папку или трек.');
  if (!list || (listChanging && list.ready)) { serverControls(); return; }
  $('server-folder').textContent = list.name || 'Медиасервер';
  if (list.ready && serverFolder !== list.folder) {
    resetServerBrowser();
    serverFolder = list.folder;
  }
  // Start each continuous list at the first item, regardless of the hardware cursor.
  if (list.ready && !serverItems.size && list.start !== 1) {
    serverControls();
    queueMicrotask(() => preloadServerItems(1));
    return;
  }
  if (list.ready) for (const item of list.items) serverItems.set(item.index, item);
  const items = [];
  for (let index = 1; serverItems.has(index); index++) items.push(serverItems.get(index));
  const signature = JSON.stringify([serverFolder, items]);
  if ($('server-list').dataset.context !== signature) {
    const position = $('server-list').scrollTop;
    $('server-list').dataset.context = signature;
    const existing = new Map([...$('server-list').children].map(button => [Number(button.dataset.serverLine), button]));
    for (const item of items) {
      const key = JSON.stringify(item);
      const previous = existing.get(item.index);
      if (previous?.dataset.itemKey === key) continue;
      const button = document.createElement('button');
      button.className = 'server-item';
      button.dataset.serverControl = '';
      button.dataset.serverLine = item.index;
      button.dataset.itemKey = key;
      button.dataset.selectable = String(item.selectable);
      const icon = document.createElement('span');
      icon.className = 'server-item-icon';
      icon.setAttribute('aria-hidden', 'true');
      icon.textContent = item.kind === 'Container' ? '▱' : '♫';
      const label = document.createElement('span');
      label.textContent = item.label;
      const arrow = document.createElement('span');
      arrow.className = 'server-item-arrow';
      arrow.setAttribute('aria-hidden', 'true');
      arrow.textContent = item.kind === 'Container' ? '→' : '▶';
      button.append(icon, label, arrow);
      const folder = serverFolder;
      button.addEventListener('click', () => {
        if (item.kind === 'Container') resetServerBrowser();
        command('server_item', {target: item.index, folder, label: item.label});
      });
      if (previous) previous.replaceWith(button);
      else $('server-list').append(button);
    }
    $('server-list').scrollTop = position;
  }
  $('server-list').dataset.loaded = items.length;
  $('server-count').textContent = `${items.length} из ${list.total}`;
  serverControls();
  if (list.ready) queueMicrotask(loadMoreServerItems);
}

function resetServerBrowser() {
  cancelServerPreload();
  serverFolder = '';
  serverItems.clear();
  $('server-list').replaceChildren();
  $('server-list').scrollTop = 0;
  delete $('server-list').dataset.context;
  $('server-list').dataset.loaded = '0';
  $('server-count').textContent = '';
}

function loadMoreServerItems() {
  if (serverPreload || serverPreloadTimer || document.hidden) return;
  serverPreloadTimer = setTimeout(() => {
    serverPreloadTimer = null;
    void preloadServerItems();
  }, 200);
}

function cancelServerPreload() {
  clearTimeout(serverPreloadTimer);
  serverPreloadTimer = null;
  serverPreload?.abort();
  serverPreload = null;
}

async function preloadServerItems(target = null) {
  const list = serverData.list, loaded = Number($('server-list').dataset.loaded || 0);
  if (serverPreload || !online || state?.input !== 'SERVER' || state.power !== 'On' || !list?.ready || listChanging || pending.has('server_list') || pending.has('source')) return;
  if (target === null && (!loaded || loaded >= list.total)) return;
  const request = new AbortController(), folder = list.folder;
  serverPreload = request;
  serverRequests.list?.abort();
  serverRequests.list = null;
  try {
    await api('/api/command', {action: 'server_jump', value: {target: target ?? loaded + 1, folder}}, request.signal);
    const deadline = Date.now() + 8000;
    while (serverPreload === request) {
      const data = await api('/api/server/list', null, request.signal);
      if (serverPreload !== request) return;
      if (!data.active || data.list.folder !== folder) return;
      if (data.list.ready) {
        serverData.list = data.list;
        serverErrors.list = '';
        renderServer();
        return;
      }
      if (Date.now() >= deadline) throw new Error('Background list is still busy');
      await new Promise(resolve => {
        const timer = setTimeout(resolve, 350);
        request.signal.addEventListener('abort', () => { clearTimeout(timer); resolve(); }, {once: true});
      });
    }
  } catch (e) {
    // A failed speculative page does not disable already loaded entries.
    if (serverPreload === request && e.name !== 'AbortError') {
      serverPreloadTimer = setTimeout(() => { serverPreloadTimer = null; void preloadServerItems(target); }, 5000);
    }
  } finally {
    if (serverPreload === request) {
      serverPreload = null;
      loadMoreServerItems();
    }
  }
}

async function refreshServerPart(part, force = false) {
  if (part === 'list' && serverPreload) return;
  if (!online || state?.input !== 'SERVER' || state.power !== 'On' || pending.has('source') || pending.has('server_' + part)) return;
  if (serverRequests[part] && !force) return;
  serverRequests[part]?.abort();
  const request = new AbortController();
  serverRequests[part] = request;
  if (part === 'list') clearTimeout(serverRetry);
  try {
    const data = await api('/api/server/' + part, null, request.signal);
    if (serverRequests[part] !== request) return;
    serverData[part] = data.active ? data[part] : null;
    serverErrors[part] = data.active ? '' : 'Выберите SERVER на включённом ресивере.';
    if (part === 'list') listChanging = !!data.active && !data.list.ready;
    renderServer();
    if (part === 'list' && listChanging) serverRetry = setTimeout(() => refreshServerPart('list'), 750);
  } catch (e) {
    if (serverRequests[part] !== request || e.name === 'AbortError') return;
    serverErrors[part] = e.name === 'TimeoutError' ? 'Медиасервер не ответил вовремя. Повторяем подключение…' : e.message;
    renderServer();
  } finally { if (serverRequests[part] === request) serverRequests[part] = null; }
}

function refreshServer() {
  void refreshServerPart('list');
  void refreshServerPart('play');
}

function tunerControls() {
  const available = online && state?.input === 'TUNER' && state.power === 'On' && !pending.has('source') && !pending.has('tuner') && tunerData.play?.available && !tunerErrors.play;
  document.querySelectorAll('[data-tuner-control]').forEach(el => el.disabled = !available);
  if (!available) return;
  const play = tunerData.play, limits = play.band === 'FM' ? [87.5, 108] : [531, 1611];
  document.querySelectorAll('[data-tuner-step]').forEach(button => button.disabled = play.frequency === null || (Number(button.dataset.tunerStep) < 0 ? play.frequency <= limits[0] : play.frequency >= limits[1]));
  document.querySelectorAll('#tuner-preset, [data-tuner-preset]').forEach(el => el.disabled = !tunerData.presets?.length || !!tunerErrors.presets);
}

function renderTuner() {
  const play = tunerData.play;
  $('tuner-message').textContent = tunerErrors.play || (state?.power !== 'On' ? 'Включите ресивер для управления радио.' : !play ? 'Загрузка тюнера…' : '');
  $('tuner-signal').textContent = !play ? 'ЗАГРУЗКА…' : play.tuned ? (play.stereo ? 'СТЕРЕО · СИГНАЛ' : 'МОНО · СИГНАЛ') : 'ПОИСК СИГНАЛА';
  $('tuner-frequency-value').textContent = play?.frequency == null ? '—' : play.frequency.toLocaleString('ru-RU', {minimumFractionDigits: play.band === 'FM' ? 2 : 0, maximumFractionDigits: 2});
  $('tuner-unit').textContent = play?.band === 'AM' ? 'kHz' : 'MHz';
  $('tuner-station').textContent = play?.station || 'Радиостанция';
  $('tuner-text').textContent = [play?.type, play?.text].filter(Boolean).join(' · ');
  document.querySelectorAll('[data-tuner-band]').forEach(button => button.setAttribute('aria-pressed', String(button.dataset.tunerBand === play?.band)));
  if (play) {
    const fm = play.band === 'FM', input = $('tuner-frequency');
    const bandChanged = input.dataset.band !== play.band;
    input.dataset.band = play.band;
    input.min = fm ? 87.5 : 531;
    input.max = fm ? 108 : 1611;
    input.step = fm ? 0.05 : 9;
    if (bandChanged || document.activeElement !== input) input.value = play.frequency ?? '';
  }
  if (tunerData.presets) {
    const signature = JSON.stringify(tunerData.presets);
    if ($('tuner-preset').dataset.signature !== signature) {
      $('tuner-preset').dataset.signature = signature;
      $('tuner-preset').replaceChildren(new Option('Выберите станцию', ''), ...tunerData.presets.map(item => new Option(item.label, item.value)));
    }
    $('tuner-preset').value = play?.preset ?? '';
  }
  $('tuner-presets-message').textContent = tunerErrors.presets || (tunerData.presets ? tunerData.presets.length ? '' : 'В ресивере нет сохранённых станций.' : 'Загрузка станций…');
  tunerControls();
}

async function refreshTunerPart(part) {
  if (!online || state?.input !== 'TUNER' || state.power !== 'On' || pending.has('source') || pending.has('tuner') || tunerRequests[part]) return;
  const request = new AbortController();
  tunerRequests[part] = request;
  try {
    const data = await api('/api/tuner/' + part, null, request.signal);
    if (tunerRequests[part] !== request) return;
    tunerData[part] = data.active ? data[part] : null;
    tunerErrors[part] = data.active ? '' : 'Выберите TUNER на включённом ресивере.';
    renderTuner();
  } catch (e) {
    if (tunerRequests[part] !== request || e.name === 'AbortError') return;
    tunerErrors[part] = e.name === 'TimeoutError' ? 'Тюнер не ответил вовремя. Повторяем подключение…' : e.message;
    renderTuner();
  } finally { if (tunerRequests[part] === request) tunerRequests[part] = null; }
}
function refreshTuner() {
  void refreshTunerPart('play');
  void refreshTunerPart('presets');
}

function serverNavigate(action, target) {
  if (serverData.list) command(action, {target, folder: serverData.list.folder});
}
function connection(ok) {
  online = ok;
  $('connection').textContent = ok ? 'Ресивер на связи' : 'Нет связи с ресивером';
  $('connection-dot').className = 'dot ' + (ok ? 'online' : 'offline');
  controls();
}
function build() {
  $('model').textContent = 'Yamaha ' + config.model;
  $('host').textContent = config.host;
  $('volume').min = config.min_volume;
  $('volume').max = config.max_volume;
  $('volume-limit').textContent = 'До ' + format(config.max_volume) + ' dB';
  const visible = config.inputs.filter(item => config.visible_inputs == null || config.visible_inputs.includes(item.value));
  $('input-count').textContent = visible.length + ' из ' + config.inputs.length;
  $('inputs').replaceChildren(...visible.map(item => {
    const button = document.createElement('button');
    button.className = 'input-button';
    button.dataset.input = item.value;
    button.dataset.control = '';
    button.dataset.action = 'input';
    button.setAttribute('aria-pressed', 'false');
    const icon = document.createElement('span');
    icon.className = 'input-icon';
    icon.setAttribute('aria-hidden', 'true');
    icon.textContent = item.value.startsWith('HDMI') ? '▣' : playbackInputs.includes(item.value) ? '♫' : item.value === 'TUNER' ? '◉' : '↗';
    button.append(icon, document.createTextNode(item.label));
    button.addEventListener('click', () => command('input', item.value));
    return button;
  }));
  $('scenes').replaceChildren(...config.scenes.map((item, index) => {
    const button = document.createElement('button');
    button.dataset.control = '';
    button.dataset.action = 'scene';
    const label = document.createElement('span');
    label.textContent = 'SCENE 0' + (index + 1);
    button.append(label, document.createTextNode(item.label));
    button.addEventListener('click', () => command('scene', item.value));
    return button;
  }));
  $('program').replaceChildren(...config.programs.map(name => new Option(name, name)));
  if ($('settings-dialog').open) renderSettingsInputs();
  render();
}
async function refreshConfig() {
  if (config || configLoading) return;
  configLoading = true;
  try {
    config = await api('/api/config');
    $('config-message').hidden = true;
    build();
  } catch (e) {
    $('config-message').textContent = 'Не удалось загрузить входы и сцены. ' + e.message;
  } finally { configLoading = false; }
}

async function refresh(force = false) {
  if (statusRequest && !force) return;
  statusRequest?.abort();
  const request = new AbortController();
  statusRequest = request;
  try {
    const data = await api('/api/status', null, request.signal);
    if (statusRequest !== request) return;
    state = data;
    connection(true);
    render();
    $('last-update').textContent = 'Обновлено ' + new Date().toLocaleTimeString('ru-RU');
    if ($('error').dataset.kind !== 'command') error();
    refreshServer();
    refreshTuner();
  } catch (e) {
    if (statusRequest !== request || e.name === 'AbortError') return;
    connection(false);
    error(e.name === 'TimeoutError' ? 'Ресивер не ответил вовремя. Повторяем подключение…' : e.message);
  } finally { if (statusRequest === request) statusRequest = null; }
}
async function command(action, value) {
  const group = commandGroup(action);
  if (pending.has(group) || !online) return;
  pending.add(group);
  if (group === 'source' || group === 'server_list') cancelServerPreload();
  statusRequest?.abort();
  if (group === 'source' || group === 'tuner') {
    for (const part of ['play', 'presets']) {
      tunerRequests[part]?.abort();
      tunerRequests[part] = null;
    }
  }
  if (group === 'source' || group === 'server_list' || group === 'server_play') {
    for (const part of ['list', 'play']) {
      if (group !== 'source' && group !== 'server_' + part) continue;
      serverRequests[part]?.abort();
      serverRequests[part] = null;
    }
  }
  if (group === 'server_list') {
    if (action === 'server_cursor') resetServerBrowser();
    listChanging = true;
    $('server-message').textContent = 'Ресивер загружает список…';
  }
  controls();
  error();
  delete $('error').dataset.kind;
  try {
    await api('/api/command', {action, value});
    await refresh(true);
  } catch (e) {
    $('error').dataset.kind = 'command';
    error(e.name === 'TimeoutError' ? 'Нет подтверждения от ресивера. Состояние будет обновлено.' : e.message);
  } finally {
    pending.delete(group);
    render();
    controls();
    refreshServer();
    refreshTuner();
  }
}
$('power').addEventListener('click', () => command('power', state.power === 'On' ? 'Standby' : 'On'));
for (const id of ['mute', 'straight', 'enhancer', 'direct', 'drc']) $(id).addEventListener('click', () => command(id, !state[id]));
for (const id of ['volume', 'bass', 'treble']) {
  $(id).addEventListener('input', () => $(id + '-value').textContent = format(Number($(id).value)) + (id === 'volume' ? '' : ' dB'));
  $(id).addEventListener('change', () => command(id, Number($(id).value)));
}
$('quieter').addEventListener('click', () => command('volume', Math.min(config.max_volume, Math.max(config.min_volume, state.volume - 1))));
$('louder').addEventListener('click', () => command('volume', Math.min(config.max_volume, state.volume + 1)));
$('program').addEventListener('change', () => command('program', $('program').value));
$('sleep').addEventListener('change', () => command('sleep', $('sleep').value));
document.querySelectorAll('[data-playback]').forEach(button => button.addEventListener('click', () => command('playback', button.dataset.playback)));
document.querySelectorAll('[data-server-playback]').forEach(button => button.addEventListener('click', () => command('server_playback', button.dataset.serverPlayback)));
$('server-repeat').addEventListener('change', () => command('server_repeat', $('server-repeat').value));
$('server-shuffle').addEventListener('click', () => command('server_shuffle', !serverData.play.shuffle));
$('server-back').addEventListener('click', () => serverNavigate('server_cursor', 'Return'));
$('server-home').addEventListener('click', () => serverNavigate('server_cursor', 'Return to Home'));
$('server-list').addEventListener('scroll', loadMoreServerItems, {passive: true});
document.querySelectorAll('[data-volume-preset]').forEach(button => button.addEventListener('click', () => command('volume', Number(button.dataset.volumePreset))));
document.querySelectorAll('[data-tuner-band]').forEach(button => button.addEventListener('click', () => command('tuner_band', button.dataset.tunerBand)));
document.querySelectorAll('[data-tuner-search]').forEach(button => button.addEventListener('click', () => command('tuner_search', button.dataset.tunerSearch)));
document.querySelectorAll('[data-tuner-preset]').forEach(button => button.addEventListener('click', () => command('tuner_preset', button.dataset.tunerPreset)));
document.querySelectorAll('[data-tuner-step]').forEach(button => button.addEventListener('click', () => {
  const play = tunerData.play;
  command('tuner_frequency', {band: play.band, frequency: Number((play.frequency + Number(button.dataset.tunerStep) * (play.band === 'FM' ? 0.05 : 9)).toFixed(2))});
}));
$('tuner-frequency-form').addEventListener('submit', event => {
  event.preventDefault();
  command('tuner_frequency', {band: tunerData.play.band, frequency: Number($('tuner-frequency').value)});
});
$('tuner-preset').addEventListener('change', () => { if ($('tuner-preset').value) command('tuner_preset', $('tuner-preset').value); });
let settingsData = null;
function renderSettingsInputs() {
  if (!settingsData) return;
    $('settings-inputs').replaceChildren(...(config?.inputs || []).map(item => {
      const label = document.createElement('label'), checkbox = document.createElement('input');
      checkbox.type = 'checkbox';
      checkbox.value = item.value;
      checkbox.checked = settingsData.visible_inputs == null || settingsData.visible_inputs.includes(item.value);
      label.append(checkbox, document.createTextNode(item.label));
      return label;
    }));
    $('settings-inputs-message').textContent = config ? '' : 'Сохраните IP. Список источников появится после подключения к ресиверу.';
}
$('settings-open').addEventListener('click', async () => {
  settingsData = null;
  $('settings-error').hidden = true;
  $('settings-save').disabled = true;
  $('settings-host').value = config?.host || '';
  $('settings-inputs').replaceChildren();
  $('settings-inputs-message').textContent = 'Загрузка настроек…';
  $('settings-dialog').showModal();
  try {
    settingsData = await api('/api/settings');
    $('settings-host').value = settingsData.host;
    renderSettingsInputs();
    $('settings-save').disabled = false;
  } catch (e) {
    $('settings-error').textContent = e.message;
    $('settings-error').hidden = false;
  }
});
for (const id of ['settings-close', 'settings-cancel']) $(id).addEventListener('click', () => $('settings-dialog').close());
$('settings-form').addEventListener('submit', async event => {
  event.preventDefault();
  $('settings-save').disabled = true;
  $('settings-error').hidden = true;
  try {
    const visible_inputs = config ? [...$('settings-inputs').querySelectorAll('input:checked')].map(input => input.value) : settingsData.visible_inputs;
    await api('/api/settings', {host: $('settings-host').value.trim(), visible_inputs});
    location.reload();
  } catch (e) {
    $('settings-error').textContent = e.message;
    $('settings-error').hidden = false;
    $('settings-save').disabled = false;
  }
});
function refreshAll() {
  void refreshConfig();
  void refresh();
  refreshServer();
  refreshTuner();
}
$('refresh').addEventListener('click', () => { delete $('error').dataset.kind; refreshAll(); });
document.addEventListener('visibilitychange', () => { if (!document.hidden) refreshAll(); });
refreshAll();
setInterval(() => { if (!document.hidden) refreshAll(); }, 5000);
