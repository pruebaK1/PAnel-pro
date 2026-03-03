from flask import Flask, request, send_from_directory
import subprocess, threading, uuid, json, datetime, re, time, os, shutil, glob

app = Flask(__name__)

machine = {
    'status': 'running',
    'width': 1280, 'height': 720,
    'logs': [], 'started_at': datetime.datetime.utcnow().isoformat(),
    'fingerprint': {
        'user_agent': 'Mozilla/5.0 (X11; Linux x86_64; rv:121.0) Gecko/20100101 Firefox/121.0',
        'timezone': 'America/New_York',
        'language': 'en-US',
    }
}

tabs        = {}
outputs     = {}
streams     = {}
credentials = {}

PROFILE_DIR  = '/tmp/nexus_firefox_master'
HLS_DIR      = '/tmp/nexus_hls'
DISPLAY_BASE = 99
DISPLAY_MAX  = 10
display_lock = threading.Lock()

os.makedirs(PROFILE_DIR, exist_ok=True)
os.makedirs(HLS_DIR, exist_ok=True)

def ts():
    return datetime.datetime.utcnow().strftime('%H:%M:%S')

def mlog(msg):
    e = f'[{ts()}] {msg}'
    machine['logs'].append(e)
    if len(machine['logs']) > 300:
        machine['logs'] = machine['logs'][-300:]

def tlog(lst, msg):
    e = f'[{ts()}] {msg}'
    lst.append(e)
    if len(lst) > 300:
        lst[:] = lst[-300:]

def domain_from_url(url):
    try:
        from urllib.parse import urlparse
        return urlparse(url).netloc.replace('www.', '')
    except:
        return ''

def get_stream_url(url):
    if any(x in url for x in ['.m3u8', '.m3u', 'rtmp://', 'rtmps://', 'rtsp://']):
        return url
    try:
        r = subprocess.check_output(
            ['yt-dlp', '-f', 'best', '-g', '--no-playlist', url],
            text=True, timeout=30
        ).strip().split('\n')[0]
        return r or None
    except:
        return None

def alloc_display():
    used = {t.get('display_num') for t in tabs.values() if t.get('display_num')}
    for i in range(DISPLAY_BASE, DISPLAY_BASE + DISPLAY_MAX):
        if i not in used:
            return i
    return None

def start_xvfb(display_num, w, h):
    disp = f':{display_num}'
    subprocess.run(['pkill', '-f', f'Xvfb {disp}'], capture_output=True)
    time.sleep(0.5)
    proc = subprocess.Popen(
        ['Xvfb', disp, '-screen', '0', f'{w}x{h}x24', '-ac', '+extension', 'GLX'],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT
    )
    time.sleep(1.5)
    env = os.environ.copy()
    env['DISPLAY'] = disp
    try:
        subprocess.run(['xsetroot', '-solid', 'black'], env=env, capture_output=True, timeout=3)
    except:
        pass
    return proc

def start_pulse(display_num):
    sock = f'/tmp/pulse-{display_num}'
    subprocess.run(['pkill', '-f', f'pulseaudio.*{sock}'], capture_output=True)
    time.sleep(0.3)
    env = os.environ.copy()
    env['DISPLAY'] = f':{display_num}'
    subprocess.Popen(
        ['pulseaudio', '--start', '--exit-idle-time=-1'],
        env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT
    )
    time.sleep(1)

def start_vnc_for_tab(tid, display_num):
    vnc_port = 5900 + (display_num - DISPLAY_BASE)
    ws_port  = 6080 + (display_num - DISPLAY_BASE)
    disp     = f':{display_num}'
    env      = os.environ.copy()
    env['DISPLAY'] = disp
    subprocess.run(['pkill', '-f', f'x11vnc.*{disp}'], capture_output=True)
    subprocess.run(['pkill', '-f', f'websockify.*{ws_port}'], capture_output=True)
    time.sleep(0.5)
    subprocess.Popen(
        ['x11vnc', '-display', disp, '-nopw', '-listen', 'localhost',
         '-xkb', '-forever', '-shared', '-rfbport', str(vnc_port)],
        env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT
    )
    time.sleep(1.5)
    subprocess.Popen(
        ['websockify', '--web', '/app/vnc', str(ws_port), f'localhost:{vnc_port}'],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT
    )
    # Sincronizar portapapeles con xclip
    env2 = os.environ.copy()
    env2['DISPLAY'] = disp
    subprocess.Popen(
        ['bash', '-c',
         f'while true; do '
         f'xclip -selection clipboard -o 2>/dev/null | '
         f'xclip -selection primary -i 2>/dev/null; '
         f'sleep 1; done'],
        env=env2, stdout=subprocess.PIPE, stderr=subprocess.STDOUT
    )
    tab = tabs.get(tid)
    if tab:
        tab['vnc_port'] = vnc_port
        tab['ws_port']  = ws_port
        tlog(tab['logs'], f'VNC :{vnc_port}  WS :{ws_port}')

def setup_firefox_profile(tid):
    dst = f'/tmp/nexus_profile_{tid}'
    if os.path.exists(PROFILE_DIR) and os.listdir(PROFILE_DIR):
        try:
            if os.path.exists(dst):
                shutil.rmtree(dst)
            shutil.copytree(PROFILE_DIR, dst)
            return dst
        except:
            pass
    os.makedirs(dst, exist_ok=True)
    prefs_path = os.path.join(dst, 'prefs.js')
    with open(prefs_path, 'w') as f:
        f.write('user_pref("media.eme.enabled", true);\n')
        f.write('user_pref("media.gmp-widevinecdm.enabled", true);\n')
        f.write('user_pref("media.gmp-widevinecdm.visible", true);\n')
        f.write('user_pref("media.autoplay.default", 0);\n')
        f.write('user_pref("media.autoplay.blocking_policy", 0);\n')
        f.write('user_pref("full-screen-api.enabled", true);\n')
        f.write('user_pref("browser.sessionstore.resume_from_crash", false);\n')
        f.write('user_pref("privacy.resistFingerprinting", false);\n')
        f.write('user_pref("browser.tabs.crashReporting.sendReport", false);\n')
        f.write('user_pref("dom.ipc.processCount", 2);\n')
        f.write('user_pref("gfx.webrender.all", false);\n')
        f.write('user_pref("layers.acceleration.disabled", true);\n')
        f.write('user_pref("browser.cache.disk.enable", true);\n')
        f.write('user_pref("network.http.referer.defaultPolicy", 2);\n')
    return dst

def start_hls(tid):
    tab = tabs.get(tid)
    if not tab:
        return
    hls_path = os.path.join(HLS_DIR, tid)
    os.makedirs(hls_path, exist_ok=True)
    w    = str(machine['width'])
    h    = str(machine['height'])
    disp = f':{tab["display_num"]}'
    env  = os.environ.copy()
    env['DISPLAY'] = disp
    cmd = [
        'ffmpeg', '-y',
        '-f', 'x11grab', '-r', '8', '-s', f'{w}x{h}',
        '-i', f'{disp}+0,0',
        '-vf', 'scale=960:540',
        '-c:v', 'libx264', '-preset', 'ultrafast', '-tune', 'zerolatency',
        '-b:v', '700k', '-g', '16',
        '-f', 'hls', '-hls_time', '1', '-hls_list_size', '4',
        '-hls_flags', 'delete_segments+omit_endlist',
        os.path.join(hls_path, 'live.m3u8')
    ]
    proc = subprocess.Popen(cmd, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    tab['hls_proc'] = proc
    tlog(tab['logs'], f'HLS PID={proc.pid}')

def inject_credentials(tid, domain):
    cred = credentials.get(domain) or credentials.get('www.' + domain)
    if not cred or not cred.get('auto_login'):
        return
    tab = tabs.get(tid)
    if not tab:
        return
    username  = cred.get('username', '')
    password  = cred.get('password', '')
    login_url = cred.get('login_url', '')
    if not username or not password:
        return
    env = os.environ.copy()
    env['DISPLAY'] = f':{tab["display_num"]}'
    tlog(tab['logs'], f'Auto-login: {domain}')
    if login_url:
        try:
            subprocess.run(['xdotool', 'key', 'ctrl+l'], env=env, capture_output=True, timeout=3)
            time.sleep(0.4)
            subprocess.run(['xdotool', 'type', '--delay', '30', login_url], env=env, capture_output=True, timeout=8)
            subprocess.run(['xdotool', 'key', 'Return'], env=env, capture_output=True, timeout=3)
            time.sleep(4)
        except:
            pass
    try:
        subprocess.run(['xdotool', 'type', '--delay', '80', username], env=env, capture_output=True, timeout=10)
        time.sleep(0.5)
        subprocess.run(['xdotool', 'key', 'Return'], env=env, capture_output=True, timeout=3)
        time.sleep(3)
        subprocess.run(['xdotool', 'type', '--delay', '80', password], env=env, capture_output=True, timeout=10)
        time.sleep(0.5)
        subprocess.run(['xdotool', 'key', 'Return'], env=env, capture_output=True, timeout=3)
        tlog(tab['logs'], 'Credenciales inyectadas - verifica VNC si pide 2FA')
    except Exception as e:
        tlog(tab['logs'], f'xdotool error: {e}')

def _cleanup_tab(tid):
    tab = tabs.get(tid)
    if not tab:
        return
    if tab.get('hls_proc'):
        try:
            tab['hls_proc'].kill()
        except:
            pass
        tab['hls_proc'] = None
    for p in list(tab.get('output_procs', {}).values()):
        try:
            p.kill()
        except:
            pass
    for p in list(tab.get('restream_procs', {}).values()):
        try:
            p.kill()
        except:
            pass
    tab['output_procs']   = {}
    tab['restream_procs'] = {}
    dn = tab.get('display_num')
    if dn:
        ws_port = 6080 + (dn - DISPLAY_BASE)
        subprocess.run(['pkill', '-f', f'x11vnc.*:{dn}'], capture_output=True)
        subprocess.run(['pkill', '-f', f'websockify.*{ws_port}'], capture_output=True)
        subprocess.run(['pkill', '-f', f'pulseaudio'], capture_output=True)
        if tab.get('xvfb_proc'):
            try:
                tab['xvfb_proc'].kill()
            except:
                pass
        subprocess.run(['pkill', '-f', f'Xvfb :{dn}'], capture_output=True)
        tab['display_num'] = None
        tab['xvfb_proc']   = None
    try:
        shutil.rmtree(os.path.join(HLS_DIR, tid))
    except:
        pass
    try:
        shutil.rmtree(f'/tmp/nexus_profile_{tid}')
    except:
        pass

def stop_tab(tid):
    tab = tabs.get(tid)
    if not tab:
        return
    tab['stop_requested'] = True
    tab['status']         = 'stopping'
    if tab.get('chrome_proc'):
        try:
            tab['chrome_proc'].kill()
        except:
            pass
        tab['chrome_proc'] = None
    _cleanup_tab(tid)
    tab['status'] = 'stopped'
    tlog(tab['logs'], 'Detenida')

def run_tab(tid):
    tab = tabs.get(tid)
    if not tab:
        return
    with display_lock:
        dn = alloc_display()
        if dn is None:
            tlog(tab['logs'], 'No hay displays disponibles (max 10)')
            tab['status'] = 'error'
            return
        tab['display_num'] = dn
    w = machine['width']
    h = machine['height']
    tlog(tab['logs'], f'Display :{dn} asignado')
    xvfb = start_xvfb(dn, w, h)
    tab['xvfb_proc'] = xvfb
    tlog(tab['logs'], f'Xvfb PID={xvfb.pid}')
    start_pulse(dn)
    disp    = f':{dn}'
    env     = os.environ.copy()
    env['DISPLAY'] = disp
    env['MOZ_DISABLE_CONTENT_SANDBOX'] = '1'
    env['MOZ_X11_EGL'] = '0'
    env['MOZ_DISABLE_RDD_SANDBOX'] = '1'
    tab['status'] = 'loading'
    url    = tab['url']
    domain = domain_from_url(url)
    tlog(tab['logs'], f'Abriendo: {url}')
    profile_dir = setup_firefox_profile(tid)
    cmd = [
        'firefox-esr',
        '--profile', profile_dir,
        '--new-instance',
        '--no-remote',
        '--width', str(w),
        '--height', str(h),
        url
    ]
    proc = subprocess.Popen(cmd, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    tab['chrome_proc'] = proc
    tab['pid']         = proc.pid
    tlog(tab['logs'], f'Firefox PID={proc.pid}')
    load_wait = tab.get('load_wait', 8)
    for _ in range(load_wait * 2):
        if tab.get('stop_requested'):
            proc.kill()
            tab['status'] = 'stopped'
            _cleanup_tab(tid)
            return
        time.sleep(0.5)
    tab['status']     = 'running'
    tab['started_at'] = datetime.datetime.utcnow().isoformat()
    tlog(tab['logs'], 'Pestana activa')
    threading.Thread(target=start_vnc_for_tab, args=(tid, dn), daemon=True).start()
    if domain:
        threading.Thread(target=inject_credentials, args=(tid, domain), daemon=True).start()
    assigned = [oid for oid, o in outputs.items() if o.get('tab_id') in (tid, '__all__')]
    for oid in assigned:
        threading.Thread(target=run_tab_output, args=(tid, oid), daemon=True).start()
    proc.wait()
    if tab.get('stop_requested'):
        tab['status'] = 'stopped'
        _cleanup_tab(tid)
        return
    tab['status'] = 'crashed'
    tlog(tab['logs'], 'Firefox termino inesperadamente')
    if tab.get('autoretry'):
        iv = tab.get('retry_interval', 15)
        tlog(tab['logs'], f'Reconectando en {iv}s...')
        for _ in range(iv * 2):
            if tab.get('stop_requested'):
                break
            time.sleep(0.5)
        if not tab.get('stop_requested'):
            tab['stop_requested'] = False
            run_tab(tid)
    else:
        _cleanup_tab(tid)
        tab['status'] = 'stopped'

def run_tab_output(tid, oid):
    tab = tabs.get(tid)
    out = outputs.get(oid)
    if not tab or not out:
        return
    dest = out['rtmp'].rstrip('/')
    if out.get('key'):
        dest += '/' + out['key']
    w    = str(machine['width'])
    h    = str(machine['height'])
    fps  = str(tab.get('fps', 30))
    btr  = out.get('bitrate', '3000k')
    abtr = out.get('audio_bitrate', '128k')
    res  = out.get('resolution', 'source')
    disp = f':{tab["display_num"]}'
    vf   = f'scale={w}:{h}'
    if res not in ('source', 'copy', ''):
        try:
            rw, rh = res.split('x')
            vf = f'scale={rw}:{rh}:force_original_aspect_ratio=decrease,pad={rw}:{rh}:(ow-iw)/2:(oh-ih)/2:black'
        except:
            pass
    env = os.environ.copy()
    env['DISPLAY'] = disp
    cmd = [
        'ffmpeg', '-y',
        '-f', 'x11grab', '-r', fps, '-s', f'{w}x{h}',
        '-i', f'{disp}+0,0',
        '-f', 'pulse', '-ac', '2', '-i', 'default',
        '-vf', vf,
        '-c:v', 'libx264', '-preset', 'veryfast',
        '-b:v', btr, '-maxrate', btr,
        '-bufsize', str(int(btr.replace('k', '')) * 2) + 'k',
        '-pix_fmt', 'yuv420p',
        '-c:a', 'aac', '-b:a', abtr, '-ar', '44100',
        '-f', 'flv', dest
    ]
    name = out.get('name', oid)
    tlog(tab['logs'], f'[{name}] -> {dest}')
    proc = subprocess.Popen(cmd, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    if 'output_procs' not in tab:
        tab['output_procs'] = {}
    tab['output_procs'][oid] = proc
    for line in proc.stdout:
        l = line.rstrip()
        if l and ('frame=' in l or 'error' in l.lower()):
            tab['logs'] = tab['logs'][-200:] + [f'[{name}] {l}']
    proc.wait()
    tlog(tab['logs'], f'[{name}] termino rc={proc.returncode}')
    tab['output_procs'].pop(oid, None)

def run_stream_output(s, out, url):
    dest = out['rtmp'].rstrip('/')
    if out.get('key'):
        dest += '/' + out['key']
    res  = out.get('resolution', 'copy')
    btr  = out.get('bitrate', '2500k')
    abtr = out.get('audio_bitrate', '128k')
    name = out.get('name', 'out')
    headers = (
        f'User-Agent: {machine["fingerprint"]["user_agent"]}\r\n'
        'Accept: */*\r\n'
    )
    if res == 'copy':
        cmd = [
            'ffmpeg', '-re',
            '-headers', headers,
            '-i', url,
            '-c', 'copy',
            '-f', 'flv', dest
        ]
    else:
        try:
            rw, rh = res.split('x')
        except:
            rw, rh = '1280', '720'
        cmd = [
            'ffmpeg', '-re',
            '-headers', headers,
            '-i', url,
            '-vf', f'scale={rw}:{rh}:force_original_aspect_ratio=decrease',
            '-c:v', 'libx264', '-preset', 'veryfast', '-b:v', btr,
            '-c:a', 'aac', '-b:a', abtr,
            '-f', 'flv', dest
        ]
    tlog(s['logs'], f'[{name}] -> {dest}')
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    s['procs'][name] = proc
    for line in proc.stdout:
        l = line.rstrip()
        if l and ('frame=' in l or 'error' in l.lower()):
            s['logs'] = s['logs'][-200:] + [f'[{name}] {l}']
    proc.wait()
    tlog(s['logs'], f'[{name}] rc={proc.returncode}')
    s['procs'].pop(name, None)

def run_stream(sid):
    s = streams.get(sid)
    if not s:
        return

    # Stream tipo screen: ya esta corriendo via restream_screen, solo marcar estado
    if s.get('stream_type') == 'screen' or s.get('source','').startswith('screen://'):
        s['status']     = 'running'
        s['started_at'] = datetime.datetime.utcnow().isoformat()
        tlog(s['logs'], 'Stream de pantalla activo')
        # Esperar hasta que se detenga
        while not s.get('stop_requested'):
            # Verificar si el ffmpeg sigue corriendo
            tab_id    = s.get('tab_id')
            output_id = s.get('output_id')
            if tab_id and output_id:
                t = tabs.get(tab_id)
                if t:
                    proc = t.get('restream_procs', {}).get(output_id)
                    if proc and proc.poll() is not None:
                        tlog(s['logs'], 'FFmpeg termino')
                        break
            time.sleep(2)
        s['status']         = 'stopped'
        s['stop_requested'] = False
        return

    while True:
        if s.get('stop_requested'):
            break
        s['status'] = 'extracting'
        s['procs']  = {}
        tlog(s['logs'], 'Extrayendo URL...')
        url = get_stream_url(s['source'])
        if not url:
            tlog(s['logs'], 'No se pudo obtener URL')
            if s.get('autoretry') and not s.get('stop_requested'):
                iv = s.get('retry_interval', 30)
                s['status'] = 'retrying'
                tlog(s['logs'], f'Reintentando en {iv}s...')
                for _ in range(iv * 2):
                    if s.get('stop_requested'):
                        break
                    time.sleep(0.5)
                continue
            else:
                s['status'] = 'error'
                break
        tlog(s['logs'], 'URL obtenida')
        s['status']     = 'running'
        s['started_at'] = datetime.datetime.utcnow().isoformat()
        assigned = [o for o in outputs.values() if o.get('stream_id') == sid]
        if not assigned:
            assigned = s.get('outputs', [])
        threads = [threading.Thread(target=run_stream_output, args=(s, o, url), daemon=True) for o in assigned]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        if s.get('stop_requested'):
            break
        if s.get('autoretry'):
            iv = s.get('retry_interval', 30)
            s['status'] = 'retrying'
            tlog(s['logs'], f'Reconectando en {iv}s...')
            for _ in range(iv * 2):
                if s.get('stop_requested'):
                    break
                time.sleep(0.5)
            if s.get('stop_requested'):
                break
        else:
            s['status'] = 'stopped'
            break
    s['status']         = 'stopped'
    s['stop_requested'] = False
    s['procs']          = {}

@app.after_request
def cors(r):
    r.headers['Access-Control-Allow-Origin']  = '*'
    r.headers['Access-Control-Allow-Methods'] = 'GET,POST,PUT,DELETE,OPTIONS'
    r.headers['Access-Control-Allow-Headers'] = 'Content-Type'
    return r

def J(data, code=200):
    return app.response_class(json.dumps(data, default=str), status=code, mimetype='application/json')

def jreq():
    return request.get_json(force=True, silent=True) or {}

@app.route('/')
def index():
    return open('/app/index.html').read(), 200, {'Content-Type': 'text/html; charset=utf-8'}

@app.route('/vnc/')
@app.route('/vnc/<path:f>')
def vnc_files(f='vnc.html'):
    return send_from_directory('/app/vnc', f)

@app.route('/hls/<tid>/<path:f>')
def hls_file(tid, f):
    return send_from_directory(os.path.join(HLS_DIR, tid), f)

@app.route('/preview/<tid>')
def preview_page(tid):
    t    = tabs.get(tid, {})
    name = t.get('name', 'Preview')
    src  = f'/hls/{tid}/live.m3u8'
    return (f'''<!DOCTYPE html><html><head><meta charset=UTF-8><title>{name}</title>
<script src="https://cdn.jsdelivr.net/npm/hls.js@latest"></script>
<style>*{{margin:0;padding:0}}body{{background:#000;height:100vh;display:flex;flex-direction:column;font-family:monospace}}
.bar{{background:#0a0a0a;padding:8px 14px;display:flex;align-items:center;gap:10px;border-bottom:1px solid #181818}}
.dot{{width:7px;height:7px;border-radius:50%;background:#333}}.dot.on{{background:#00ff88;animation:p 1.5s infinite}}
@keyframes p{{0%,100%{{opacity:1}}50%{{opacity:.3}}}}h1{{flex:1;font-size:12px;color:#ccc;letter-spacing:3px}}
.st{{font-size:10px;color:#555}}.st.on{{color:#00ff88}}video{{flex:1;width:100%;object-fit:contain}}</style></head>
<body><div class="bar"><div class="dot" id="d"></div><h1>{name}</h1><span class="st" id="s">CONECTANDO</span></div>
<video id="v" autoplay muted playsinline controls></video>
<script>var v=document.getElementById("v"),d=document.getElementById("d"),s=document.getElementById("s");
function live(x){{d.className="dot"+(x?" on":"");s.className="st"+(x?" on":"");s.textContent=x?"EN VIVO":"SIN SENAL";}}
if(Hls.isSupported()){{var h=new Hls({{liveSyncDurationCount:1,manifestLoadingRetryDelay:2000,manifestLoadingMaxRetry:99}});
h.loadSource("{src}");h.attachMedia(v);
h.on(Hls.Events.MANIFEST_PARSED,function(){{v.play().catch(function(){{}});live(true);}});
h.on(Hls.Events.ERROR,function(e,dd){{if(dd.fatal){{live(false);setTimeout(function(){{h.loadSource("{src}");}},3000);}}}});}}
else if(v.canPlayType("application/vnd.apple.mpegurl")){{v.src="{src}";v.play().catch(function(){{}});live(true);}}
</script></body></html>'''), 200, {'Content-Type': 'text/html'}

@app.route('/api/machine', methods=['GET', 'OPTIONS'])
def api_machine():
    if request.method == 'OPTIONS': return '', 204
    return J({**{k: machine[k] for k in ('status','width','height','fingerprint','started_at')},
              'logs': machine['logs'][-60:],
              'tabs_active':    sum(1 for t in tabs.values() if t['status'] == 'running'),
              'streams_active': sum(1 for s in streams.values() if s['status'] == 'running'),
              'displays_used':  sum(1 for t in tabs.values() if t.get('display_num')),
              'displays_free':  DISPLAY_MAX - sum(1 for t in tabs.values() if t.get('display_num'))})

@app.route('/api/machine/config', methods=['PUT', 'OPTIONS'])
def api_machine_config():
    if request.method == 'OPTIONS': return '', 204
    d = jreq()
    if 'width'  in d: machine['width']  = int(d['width'])
    if 'height' in d: machine['height'] = int(d['height'])
    if 'fingerprint' in d: machine['fingerprint'].update(d['fingerprint'])
    return J({'ok': True})

@app.route('/api/credentials', methods=['GET', 'OPTIONS'])
def api_creds():
    if request.method == 'OPTIONS': return '', 204
    return J({d: {**{k: v for k, v in c.items() if k != 'password'}, 'has_password': bool(c.get('password'))}
              for d, c in credentials.items()})

@app.route('/api/credentials/<domain>', methods=['GET', 'PUT', 'DELETE', 'OPTIONS'])
def api_cred(domain):
    if request.method == 'OPTIONS': return '', 204
    if request.method == 'DELETE':
        credentials.pop(domain, None); return J({'ok': True})
    if request.method == 'GET':
        c = credentials.get(domain, {})
        return J({**c, 'password': '........' if c.get('password') else ''})
    d = jreq()
    if domain not in credentials: credentials[domain] = {}
    if d.get('password') and d['password'] != '........':
        credentials[domain]['password'] = d['password']
    for k in ['username', 'login_url', 'notes', 'auto_login']:
        if k in d: credentials[domain][k] = d[k]
    credentials[domain].update({'domain': domain, 'updated_at': datetime.datetime.utcnow().isoformat()})
    return J({'ok': True})

@app.route('/api/credentials/sync', methods=['POST', 'OPTIONS'])
def api_sync_cookies():
    if request.method == 'OPTIONS': return '', 204
    d       = jreq()
    src_tid = d.get('from_tab')
    if src_tid:
        src = f'/tmp/nexus_profile_{src_tid}'
        if os.path.exists(src):
            try:
                if os.path.exists(PROFILE_DIR): shutil.rmtree(PROFILE_DIR)
                shutil.copytree(src, PROFILE_DIR)
                mlog(f'Maestro actualizado desde pestana {src_tid}')
            except Exception as e:
                return J({'error': str(e)}, 500)
    synced = []
    for tid, tab in tabs.items():
        if tid == src_tid: continue
        dst = f'/tmp/nexus_profile_{tid}'
        if os.path.exists(PROFILE_DIR):
            try:
                if os.path.exists(dst): shutil.rmtree(dst)
                shutil.copytree(PROFILE_DIR, dst)
                synced.append(tid)
            except: pass
    mlog(f'Cookies sincronizadas a {len(synced)} pestanas')
    return J({'ok': True, 'synced': synced})

@app.route('/api/tabs', methods=['GET', 'POST', 'OPTIONS'])
def api_tabs():
    if request.method == 'OPTIONS': return '', 204
    if request.method == 'GET':
        return J([{k: t[k] for k in ('id','name','url','status','fps','load_wait','autoretry','retry_interval','started_at','pid')}
                  | {'logs':       t.get('logs', [])[-60:],
                     'display_num':t.get('display_num'),
                     'vnc_port':   t.get('vnc_port'),
                     'ws_port':    t.get('ws_port'),
                     'hls_active': bool(t.get('hls_proc') and t['hls_proc'].poll() is None),
                     'extracted_urls': t.get('extracted_urls', []),
                     'extracted_title': t.get('extracted_title', '')}
                  for t in tabs.values()])
    d   = jreq()
    tid = str(uuid.uuid4())[:8]
    tabs[tid] = {
        'id': tid, 'name': d.get('name', 'Tab'), 'url': d['url'],
        'status': 'stopped', 'fps': d.get('fps', 30),
        'load_wait': d.get('load_wait', 8),
        'autoretry': d.get('autoretry', False),
        'retry_interval': d.get('retry_interval', 15),
        'chrome_proc': None, 'xvfb_proc': None,
        'hls_proc': None, 'output_procs': {}, 'restream_procs': {},
        'logs': [], 'stop_requested': False,
        'started_at': None, 'pid': None,
        'display_num': None, 'vnc_port': None, 'ws_port': None,
        'extracted_urls': [], 'extracted_title': ''
    }
    return J({'ok': True, 'id': tid})

@app.route('/api/tabs/<tid>', methods=['PUT', 'DELETE', 'OPTIONS'])
def api_tab(tid):
    if request.method == 'OPTIONS': return '', 204
    if request.method == 'DELETE':
        stop_tab(tid); tabs.pop(tid, None); return J({'ok': True})
    t = tabs.get(tid)
    if not t: return J({'error': 'No encontrada'}, 404)
    if t['status'] in ('running', 'loading'):
        return J({'error': 'Deten la pestana antes de editar'}, 400)
    d = jreq()
    for k in ['name', 'url', 'fps', 'load_wait', 'autoretry', 'retry_interval']:
        if k in d: t[k] = d[k]
    return J({'ok': True})

@app.route('/api/tabs/<tid>/start', methods=['POST', 'OPTIONS'])
def api_tab_start(tid):
    if request.method == 'OPTIONS': return '', 204
    t = tabs.get(tid)
    if not t: return J({'error': 'No encontrada'}, 404)
    if t['status'] in ('running', 'loading'): return J({'error': 'Ya activa'})
    used = sum(1 for x in tabs.values() if x.get('display_num'))
    if used >= DISPLAY_MAX: return J({'error': f'Maximo {DISPLAY_MAX} pestanas'}, 400)
    t['stop_requested'] = False
    t['logs'] = [f'[{ts()}] Iniciando...']
    threading.Thread(target=run_tab, args=(tid,), daemon=True).start()
    return J({'ok': True})

@app.route('/api/tabs/<tid>/stop', methods=['POST', 'OPTIONS'])
def api_tab_stop(tid):
    if request.method == 'OPTIONS': return '', 204
    stop_tab(tid); return J({'ok': True})

@app.route('/api/tabs/<tid>/logs', methods=['GET', 'OPTIONS'])
def api_tab_logs(tid):
    if request.method == 'OPTIONS': return '', 204
    t = tabs.get(tid)
    return J({'logs': t.get('logs', []) if t else []})

@app.route('/api/tabs/<tid>/vnc', methods=['GET', 'OPTIONS'])
def api_tab_vnc(tid):
    if request.method == 'OPTIONS': return '', 204
    t = tabs.get(tid)
    if not t or not t.get('ws_port'):
        return J({'error': 'VNC no disponible'}, 404)
    return J({'ws_port': t['ws_port'], 'vnc_port': t['vnc_port']})

@app.route('/api/tabs/<tid>/hls/start', methods=['POST', 'OPTIONS'])
def api_tab_hls_start(tid):
    if request.method == 'OPTIONS': return '', 204
    t = tabs.get(tid)
    if not t: return J({'error': 'No encontrada'}, 404)
    if t.get('hls_proc') and t['hls_proc'].poll() is None:
        return J({'ok': True, 'msg': 'Ya grabando'})
    if t['status'] != 'running':
        return J({'error': 'Pestana no activa'}, 400)
    threading.Thread(target=start_hls, args=(tid,), daemon=True).start()
    return J({'ok': True})

@app.route('/api/tabs/<tid>/hls/stop', methods=['POST', 'OPTIONS'])
def api_tab_hls_stop(tid):
    if request.method == 'OPTIONS': return '', 204
    t = tabs.get(tid)
    if not t: return J({'ok': True})
    if t.get('hls_proc'):
        try: t['hls_proc'].kill()
        except: pass
        t['hls_proc'] = None
    try: shutil.rmtree(os.path.join(HLS_DIR, tid))
    except: pass
    return J({'ok': True})

@app.route('/api/tabs/<tid>/extract', methods=['POST', 'OPTIONS'])
def api_tab_extract(tid):
    if request.method == 'OPTIONS': return '', 204
    t = tabs.get(tid)
    if not t: return J({'error': 'No encontrada'}, 404)
    if t['status'] != 'running':
        return J({'error': 'Pestana no activa'}, 400)

    def do_extract():
        tlog(t['logs'], 'Extrayendo URL de video...')
        tab_profile = f'/tmp/nexus_profile_{tid}'
        url = t.get('url', '')
        tlog(t['logs'], f'Analizando: {url}')
        found = []

        tlog(t['logs'], 'Buscando en cache del navegador...')
        try:
            for f_path in glob.glob(f'{tab_profile}/**/*', recursive=True):
                try:
                    if os.path.getsize(f_path) > 50000:
                        continue
                    with open(f_path, 'rb') as cf:
                        data = cf.read(3000)
                        text = data.decode('utf-8', errors='ignore')
                        matches = re.findall(r'https?://[^\s<>"\']+\.m3u8[^\s<>"\']*', text)
                        for m in matches:
                            if m not in found:
                                found.append(m)
                                tlog(t['logs'], f'Cache: {m[:70]}')
                except:
                    pass
        except Exception as e:
            tlog(t['logs'], f'Cache error: {e}')

        tlog(t['logs'], 'Probando yt-dlp...')
        try:
            yt_cmd = ['yt-dlp', '--user-agent', machine['fingerprint']['user_agent'],
                      '-j', '--no-playlist', '--no-warnings', url]
            cookies_db = f'{tab_profile}/cookies.sqlite'
            if os.path.exists(cookies_db):
                yt_cmd += ['--cookies-from-browser', f'firefox:{tab_profile}']
            result = subprocess.run(yt_cmd, capture_output=True, text=True, timeout=25)
            if result.returncode == 0:
                info    = json.loads(result.stdout)
                formats = info.get('formats', [])
                for fmt in formats:
                    if fmt.get('url') and fmt.get('vcodec') != 'none':
                        found.append({
                            'format_id': fmt.get('format_id', ''),
                            'ext':       fmt.get('ext', ''),
                            'quality':   fmt.get('format_note', str(fmt.get('height', ''))),
                            'url':       fmt.get('url', ''),
                            'vcodec':    fmt.get('vcodec', ''),
                            'acodec':    fmt.get('acodec', ''),
                            'tbr':       fmt.get('tbr', 0),
                        })
                tlog(t['logs'], f'yt-dlp: {len(formats)} formatos')
        except Exception as e:
            tlog(t['logs'], f'yt-dlp: {e}')

        tlog(t['logs'], 'Curl + regex...')
        try:
            r = subprocess.run(
                ['curl', '-s', '-L', '--max-time', '10',
                 '-H', f'User-Agent: {machine["fingerprint"]["user_agent"]}',
                 '-H', f'Referer: {url}', url],
                capture_output=True, text=True, timeout=15
            )
            for pat in [r'https?://[^\s<>]+\.m3u8[^\s<>]*', r'https?://[^\s<>]+/manifest[^\s<>]*']:
                for match in re.findall(pat, r.stdout):
                    clean = match.strip().rstrip("',;\"")
                    if clean and clean not in [x if isinstance(x, str) else x.get('url','') for x in found]:
                        found.append(clean)
                        tlog(t['logs'], f'Regex: {clean[:70]}')
        except Exception as e:
            tlog(t['logs'], f'Curl: {e}')

        video_urls = []
        seen = set()
        for item in found:
            if isinstance(item, str):
                if item not in seen:
                    seen.add(item)
                    video_urls.append({'format_id':'direct','ext':'m3u8','quality':'AUTO','url':item,'vcodec':'','acodec':'','tbr':0})
            elif isinstance(item, dict):
                u = item.get('url','')
                if u and u not in seen:
                    seen.add(u)
                    video_urls.append(item)

        video_urls.sort(key=lambda x: x.get('tbr') or 0, reverse=True)
        t['extracted_urls']  = video_urls[:15]
        t['extracted_title'] = url

        if video_urls:
            tlog(t['logs'], f'Encontradas {len(video_urls)} URLs')
        else:
            tlog(t['logs'], 'Sin URLs - usa STREAM PANTALLA para captura directa')
            t['extracted_urls'] = []

    threading.Thread(target=do_extract, daemon=True).start()
    return J({'ok': True})

@app.route('/api/tabs/<tid>/extracted', methods=['GET', 'OPTIONS'])
def api_tab_extracted(tid):
    if request.method == 'OPTIONS': return '', 204
    t = tabs.get(tid)
    if not t: return J({'urls': [], 'title': ''})
    return J({'urls': t.get('extracted_urls', []), 'title': t.get('extracted_title', '')})

@app.route('/api/tabs/<tid>/restream', methods=['POST', 'OPTIONS'])
def api_tab_restream(tid):
    if request.method == 'OPTIONS': return '', 204
    t = tabs.get(tid)
    if not t: return J({'error': 'No encontrada'}, 404)
    d          = jreq()
    stream_url = d.get('url')
    output_id  = d.get('output_id')
    if not stream_url: return J({'error': 'URL requerida'}, 400)
    if not output_id or output_id not in outputs:
        return J({'error': 'Salida RTMP no encontrada'}, 400)
    out  = outputs[output_id]
    dest = out['rtmp'].rstrip('/')
    if out.get('key'): dest += '/' + out['key']
    btr  = out.get('bitrate', '3000k')
    abtr = out.get('audio_bitrate', '128k')
    res  = out.get('resolution', 'copy')
    name = out.get('name', output_id)
    headers = (
        f'User-Agent: {machine["fingerprint"]["user_agent"]}\r\n'
        f'Referer: {t.get("url","")}\r\n'
        'Accept: */*\r\n'
    )

    def do_restream():
        if res == 'copy':
            cmd = ['ffmpeg', '-re', '-headers', headers, '-i', stream_url, '-c', 'copy', '-f', 'flv', dest]
        else:
            try: rw, rh = res.split('x')
            except: rw, rh = '1280', '720'
            cmd = [
                'ffmpeg', '-re', '-headers', headers, '-i', stream_url,
                '-vf', f'scale={rw}:{rh}:force_original_aspect_ratio=decrease,pad={rw}:{rh}:(ow-iw)/2:(oh-ih)/2:black',
                '-c:v', 'libx264', '-preset', 'veryfast',
                '-b:v', btr, '-maxrate', btr,
                '-bufsize', str(int(btr.replace('k',''))*2)+'k',
                '-pix_fmt', 'yuv420p',
                '-c:a', 'aac', '-b:a', abtr, '-ar', '44100',
                '-f', 'flv', dest
            ]
        if 'restream_procs' not in t: t['restream_procs'] = {}
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        t['restream_procs'][output_id] = proc
        tlog(t['logs'], f'Restream [{name}] PID={proc.pid} -> {dest}')
        for line in proc.stdout:
            l = line.rstrip()
            if l and ('frame=' in l or 'error' in l.lower() or 'fps=' in l):
                t['logs'] = t['logs'][-200:] + [f'[{name}] {l}']
        proc.wait()
        tlog(t['logs'], f'Restream [{name}] termino rc={proc.returncode}')
        t['restream_procs'].pop(output_id, None)

    threading.Thread(target=do_restream, daemon=True).start()
    tlog(t['logs'], f'Iniciando restream hacia {name}')
    return J({'ok': True})

@app.route('/api/tabs/<tid>/restream_screen', methods=['POST', 'OPTIONS'])
def api_restream_screen(tid):
    if request.method == 'OPTIONS': return '', 204
    t = tabs.get(tid)
    if not t: return J({'error': 'No encontrada'}, 404)
    if t['status'] != 'running':
        return J({'error': 'Pestana no activa'}, 400)
    d         = jreq()
    output_id = d.get('output_id')
    if not output_id or output_id not in outputs:
        return J({'error': 'Salida RTMP no encontrada'}, 400)
    out  = outputs[output_id]
    dest = out['rtmp'].rstrip('/')
    if out.get('key'): dest += '/' + out['key']
    btr  = out.get('bitrate', '3000k')
    abtr = out.get('audio_bitrate', '128k')
    res  = out.get('resolution', 'source')
    name = out.get('name', output_id)
    fps  = str(t.get('fps', 30))
    w    = str(machine['width'])
    h    = str(machine['height'])
    disp = f':{t["display_num"]}'
    vf   = f'scale={w}:{h}'
    if res not in ('source', 'copy', ''):
        try:
            rw, rh = res.split('x')
            vf = f'scale={rw}:{rh}:force_original_aspect_ratio=decrease,pad={rw}:{rh}:(ow-iw)/2:(oh-ih)/2:black'
        except: pass
    env = os.environ.copy()
    env['DISPLAY'] = disp
    if 'restream_procs' not in t: t['restream_procs'] = {}
    if output_id in t['restream_procs']:
        try: t['restream_procs'][output_id].kill()
        except: pass

    def do_screen():
        cmd = [
            'ffmpeg', '-y',
            '-f', 'x11grab', '-r', fps, '-s', f'{w}x{h}', '-i', f'{disp}+0,0',
            '-f', 'pulse', '-ac', '2', '-i', 'default',
            '-vf', vf,
            '-c:v', 'libx264', '-preset', 'veryfast',
            '-b:v', btr, '-maxrate', btr,
            '-bufsize', str(int(btr.replace('k',''))*2)+'k',
            '-pix_fmt', 'yuv420p',
            '-c:a', 'aac', '-b:a', abtr, '-ar', '44100',
            '-f', 'flv', dest
        ]
        proc = subprocess.Popen(cmd, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        t['restream_procs'][output_id] = proc
        tlog(t['logs'], f'Screen restream [{name}] PID={proc.pid}')
        for line in proc.stdout:
            l = line.rstrip()
            if l and ('frame=' in l or 'error' in l.lower() or 'fps=' in l):
                t['logs'] = t['logs'][-200:] + [f'[{name}] {l}']
        proc.wait()
        tlog(t['logs'], f'Screen restream [{name}] termino rc={proc.returncode}')
        t['restream_procs'].pop(output_id, None)

    threading.Thread(target=do_screen, daemon=True).start()
    return J({'ok': True})

@app.route('/api/tabs/<tid>/restream_screen/stop', methods=['POST', 'OPTIONS'])
def api_restream_screen_stop(tid):
    if request.method == 'OPTIONS': return '', 204
    t = tabs.get(tid)
    if not t: return J({'ok': True})
    d = jreq()
    output_id = d.get('output_id')
    if output_id and output_id in t.get('restream_procs', {}):
        try: t['restream_procs'][output_id].kill()
        except: pass
        t['restream_procs'].pop(output_id, None)
        tlog(t['logs'], 'Screen restream detenido')
    return J({'ok': True})

@app.route('/api/tabs/<tid>/export_cookies', methods=['POST', 'OPTIONS'])
def api_export_cookies(tid):
    if request.method == 'OPTIONS': return '', 204
    t = tabs.get(tid)
    if not t: return J({'error': 'No encontrada'}, 404)
    d      = jreq()
    domain = d.get('domain', 'google.com')
    tab_profile = f'/tmp/nexus_profile_{tid}'
    cookies_db  = os.path.join(tab_profile, 'cookies.sqlite')
    if not os.path.exists(cookies_db):
        if os.path.exists(PROFILE_DIR):
            try:
                if os.path.exists(PROFILE_DIR): shutil.rmtree(PROFILE_DIR)
                shutil.copytree(tab_profile, PROFILE_DIR)
                mlog(f'Maestro actualizado desde {tid}')
                return J({'ok': True, 'msg': 'Perfil guardado como maestro'})
            except Exception as e:
                return J({'error': str(e)}, 500)
        return J({'error': 'No hay cookies aun'}, 404)
    try:
        import shutil as sh
        tmp = f'/tmp/cookies_export_{tid}'
        sh.copy2(cookies_db, tmp)
        result = subprocess.run(
            ['sqlite3', tmp, f"SELECT name,value,host,path FROM moz_cookies WHERE host LIKE '%{domain.split('.')[-2]}%'"],
            capture_output=True, text=True, timeout=10
        )
        os.remove(tmp)
        cookies = []
        for line in result.stdout.strip().split('\n'):
            if not line: continue
            parts = line.split('|')
            if len(parts) >= 4:
                cookies.append({'name': parts[0], 'value': parts[1], 'domain': parts[2], 'path': parts[3]})
        if domain not in credentials: credentials[domain] = {'domain': domain}
        credentials[domain]['cookies']    = json.dumps(cookies)
        credentials[domain]['auto_login'] = True
        credentials[domain]['updated_at'] = datetime.datetime.utcnow().isoformat()
        if os.path.exists(PROFILE_DIR): shutil.rmtree(PROFILE_DIR)
        shutil.copytree(tab_profile, PROFILE_DIR)
        mlog(f'Cookies + perfil guardados: {domain}')
        return J({'ok': True, 'cookies_count': len(cookies), 'domain': domain})
    except Exception as e:
        return J({'error': str(e)}, 500)

@app.route('/api/outputs', methods=['GET', 'POST', 'OPTIONS'])
def api_outputs():
    if request.method == 'OPTIONS': return '', 204
    if request.method == 'GET': return J(list(outputs.values()))
    d   = jreq()
    oid = str(uuid.uuid4())[:8]
    outputs[oid] = {
        'id': oid, 'name': d.get('name', 'Salida'),
        'rtmp': d['rtmp'], 'key': d.get('key', ''),
        'resolution': d.get('resolution', 'source'),
        'bitrate': d.get('bitrate', '3000k'),
        'audio_bitrate': d.get('audio_bitrate', '128k'),
        'tab_id': d.get('tab_id'), 'stream_id': d.get('stream_id'),
        'created_at': datetime.datetime.utcnow().isoformat()
    }
    return J({'ok': True, 'id': oid})

@app.route('/api/outputs/<oid>', methods=['PUT', 'DELETE', 'OPTIONS'])
def api_output(oid):
    if request.method == 'OPTIONS': return '', 204
    if request.method == 'DELETE':
        outputs.pop(oid, None); return J({'ok': True})
    o = outputs.get(oid)
    if not o: return J({'error': 'No encontrada'}, 404)
    o.update(jreq()); return J({'ok': True})

@app.route('/api/streams', methods=['GET', 'POST', 'OPTIONS'])
def api_streams():
    if request.method == 'OPTIONS': return '', 204
    if request.method == 'GET':
        return J([{k: s[k] for k in ('id','name','source','status','autoretry','retry_interval','started_at')}
                  | {'logs':        s.get('logs', [])[-60:],
                     'tab_id':      s.get('tab_id'),
                     'output_id':   s.get('output_id'),
                     'stream_type': s.get('stream_type','url')}
                  for s in streams.values()])
    d   = jreq()
    sid = str(uuid.uuid4())[:8]
    streams[sid] = {
        'id': sid, 'name': d.get('name', 'Stream'),
        'source': d.get('source',''), 'status': 'stopped',
        'procs': {}, 'logs': [],
        'autoretry': d.get('autoretry', False),
        'retry_interval': d.get('retry_interval', 30),
        'stop_requested': False, 'started_at': None,
        'outputs': d.get('outputs', []),
        'tab_id':      d.get('tab_id'),
        'output_id':   d.get('output_id'),
        'stream_type': d.get('stream_type', 'url'),
        'bitrate':     d.get('bitrate', '3000k'),
    }
    return J({'ok': True, 'id': sid})

@app.route('/api/streams/<sid>', methods=['PUT', 'DELETE', 'OPTIONS'])
def api_stream(sid):
    if request.method == 'OPTIONS': return '', 204
    if request.method == 'DELETE':
        s = streams.pop(sid, None)
        if s:
            s['stop_requested'] = True
            for p in list(s.get('procs', {}).values()):
                try: p.kill()
                except: pass
        return J({'ok': True})
    s = streams.get(sid)
    if not s: return J({'error': 'No encontrado'}, 404)
    if s['status'] in ('running', 'extracting'):
        return J({'error': 'Deten antes de editar'}, 400)
    d = jreq()
    for k in ['name', 'source', 'autoretry', 'retry_interval']:
        if k in d: s[k] = d[k]
    return J({'ok': True})

@app.route('/api/streams/<sid>/start', methods=['POST', 'OPTIONS'])
def api_stream_start(sid):
    if request.method == 'OPTIONS': return '', 204
    s = streams.get(sid)
    if not s: return J({'error': 'No encontrado'}, 404)
    if s['status'] in ('running', 'extracting', 'retrying'): return J({'error': 'Ya corriendo'})
    s['stop_requested'] = False
    s['logs'] = [f'[{ts()}] Iniciando...']
    threading.Thread(target=run_stream, args=(sid,), daemon=True).start()
    return J({'ok': True})

@app.route('/api/streams/<sid>/stop', methods=['POST', 'OPTIONS'])
def api_stream_stop(sid):
    if request.method == 'OPTIONS': return '', 204
    s = streams.get(sid)
    if s:
        s['stop_requested'] = True; s['autoretry'] = False
        for p in list(s.get('procs', {}).values()):
            try: p.kill()
            except: pass
        s['procs'] = {}; s['status'] = 'stopped'
    return J({'ok': True})

@app.route('/api/streams/<sid>/logs', methods=['GET', 'OPTIONS'])
def api_stream_logs(sid):
    if request.method == 'OPTIONS': return '', 204
    s = streams.get(sid)
    return J({'logs': s.get('logs', []) if s else []})

if __name__ == '__main__':
    mlog('NEXUS v3 Firefox - iniciado')
    app.run(host='0.0.0.0', port=8080, threaded=True)
