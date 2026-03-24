from flask import Flask, request, send_from_directory
import subprocess, threading, uuid, json, datetime, time, os, shutil, re

app = Flask(__name__)

computers = {}
outputs   = {}
streams   = {}
machine   = {'status':'running','logs':[],'started_at':datetime.datetime.utcnow().isoformat()}

DISPLAY_BASE  = 99
MAX_COMPUTERS = 30
WIN_W, WIN_H  = 1280, 720
HLS_DIR       = '/tmp/nexus_hls'

os.makedirs(HLS_DIR, exist_ok=True)
os.makedirs('/app/data', exist_ok=True)
display_lock = threading.Lock()

# ─── PERSISTENCIA ─────────────────────────────────────────────────────────────

def save_streams():
    data = {}
    for sid, s in streams.items():
        data[sid] = {'id':s['id'],'name':s['name'],'sources':s['sources'],'outputs':s['outputs']}
    with open('/app/data/streams.json','w') as f: json.dump(data,f)

def save_outputs():
    with open('/app/data/outputs.json','w') as f: json.dump(outputs,f)

def load_data():
    global outputs
    try:
        if os.path.exists('/app/data/outputs.json'):
            with open('/app/data/outputs.json') as f: outputs=json.load(f)
    except: pass
    try:
        if os.path.exists('/app/data/streams.json'):
            with open('/app/data/streams.json') as f: saved=json.load(f)
            for sid,d in saved.items():
                streams[sid]={'id':sid,'name':d.get('name','Stream'),
                    'sources':d.get('sources',[]),'outputs':d.get('outputs',[]),
                    'status':'stopped','logs':[],'source_urls':{},'source_status':{},
                    'output_procs':{},'stop_requested':False,
                    'current_source':None,'current_source_idx':None,
                    'target_source_idx':None,'started_at':None}
    except: pass

load_data()

# ─── UTILS ────────────────────────────────────────────────────────────────────

def ts():
    return datetime.datetime.utcnow().strftime('%H:%M:%S')

def slog(s, level, msg):
    s['logs'] = s['logs'][-200:] + [f'[{ts()}][{level}] {msg}']

def clog(c, msg):
    c['logs'].append(f'[{ts()}] {msg}')
    if len(c['logs']) > 300: c['logs'] = c['logs'][-300:]

def clog_safe(cid, msg):
    c = computers.get(cid)
    if c: clog(c, msg)

# ─── EXTRACCION ───────────────────────────────────────────────────────────────

def extract_url(source):
    url    = source.get('url','').strip()
    method = source.get('method','curl')
    # Solo retornar directo si es un stream ya extraido
    if method == 'direct' or any(x in url for x in ['.m3u8','.m3u','rtmp://','rtmps://']):
        return url
    if method == 'curl':
        try:
            html = subprocess.check_output([
                'curl','-s','-L','--max-time','10',
                '-H','User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
                '-H','Accept: */*',
                url
            ], text=True, timeout=12, stderr=subprocess.DEVNULL)
            m = re.search(r'https?://[^\s"\'<>]+\.m3u8[^\s"\'<>]*', html)
            if m: return m.group(0)
        except: pass
        return None
    if method == 'playwright':
        try:
            result = subprocess.check_output(
                ['python3','/app/pw_extract.py', url, '--json'],
                text=True, timeout=35, stderr=subprocess.DEVNULL
            ).strip()
            if result:
                import json as _json
                data = _json.loads(result)
                m3u8 = data.get('url','')
                hdrs = data.get('headers',{})
                # Construir headers string para ffmpeg (formato correcto)
                hdr_str = ''
                for k,v in hdrs.items():
                    hdr_str += f'{k}: {v}\r\n'
                if not hdr_str:
                    hdr_str = 'User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36\r\n'
                return (m3u8, hdr_str)
        except: pass
        return None
    return None

def extract_worker(sid, src_idx):
    s = streams.get(sid)
    if not s: return
    src = s['sources'][src_idx]
    time.sleep(src_idx * 5)
    while not s.get('stop_requested'):
        s['source_status'][src_idx] = 'extracting'
        result = extract_url(src)
        if result:
            if isinstance(result, tuple):
                url, headers = result
                s['source_urls'][src_idx]     = url
                s['source_headers'][src_idx]  = headers
            else:
                s['source_urls'][src_idx]     = result
                s['source_headers'][src_idx]  = ''
            s['source_status'][src_idx] = 'ready'
            slog(s,'OK',f'Fuente {src_idx+1} lista')
            for _ in range(60):
                if s.get('stop_requested'): break
                time.sleep(1)
        else:
            s['source_urls'][src_idx]   = None
            s['source_status'][src_idx] = 'fail'
            slog(s,'WARN',f'Fuente {src_idx+1} fallo, reintentando...')
            for _ in range(15):
                if s.get('stop_requested'): break
                time.sleep(1)
    s['source_status'][src_idx] = 'stopped'

def get_ready_sources(s):
    return [i for i in range(len(s['sources']))
            if s['source_urls'].get(i) and s['source_status'].get(i)=='ready']

# ─── FFMPEG STREAM ────────────────────────────────────────────────────────────

def continuous_output(sid, out_idx):
    s = streams.get(sid)
    if not s: return
    o    = s['outputs'][out_idx]
    dest = o['rtmp'].rstrip('/') + '/' + o.get('key','')
    oid  = o.get('id','out'+str(out_idx))
    bitrate = o.get('bitrate','copy')
    slog(s,'INFO',f'[{oid}] Salida iniciando')

    def kill_proc(p):
        if p:
            try: p.kill()
            except: pass
            try: p.wait(timeout=3)
            except: pass

    def start_ffmpeg(url, src_name, headers=''):
        # Extraer referer y user-agent de headers string
        referer   = 'https://cdn-live.tv/'
        ua        = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36'
        if headers:
            for line in headers.split('\r\n'):
                if line.lower().startswith('referer:'):
                    referer = line.split(':',1)[1].strip()
                elif line.lower().startswith('user-agent:'):
                    ua = line.split(':',1)[1].strip()
        base = ['ffmpeg','-y',
                '-user_agent', ua,
                '-referer', referer,
                '-fflags','+genpts+discardcorrupt',
                '-re','-i',url]
        if bitrate == 'copy':
            cmd = base + ['-c','copy',
                   '-flvflags','no_duration_filesize','-f','flv',dest]
        else:
            btr = str(bitrate)
            if not btr.endswith('k'): btr += 'k'
            buf = str(int(btr.replace('k',''))*2)+'k'
            cmd = base + [
                   '-vf','scale=1280:720:force_original_aspect_ratio=decrease,pad=1280:720:(ow-iw)/2:(oh-ih)/2,setsar=1',
                   '-r','30','-c:v','libx264','-preset','superfast','-tune','zerolatency',
                   '-b:v',btr,'-bufsize',buf,'-g','60','-sc_threshold','0',
                   '-pix_fmt','yuv420p',
                   '-c:a','aac','-b:a','128k','-ar','44100','-ac','2',
                   '-avoid_negative_ts','make_zero',
                   '-flvflags','no_duration_filesize','-f','flv',dest]
        proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        s['output_procs'][out_idx]  = proc
        s['current_source']         = src_name
        s['current_source_idx']     = out_idx
        s['target_source_idx']      = None
        slog(s,'STREAM',f'[{oid}] Usando: {src_name[:50]} | URL: {url[:80]}')
        return proc

    for _ in range(60):
        if s.get('stop_requested'): break
        if get_ready_sources(s): break
        time.sleep(1)
    if s.get('stop_requested'): return

    current_proc = None
    ready = get_ready_sources(s)
    if ready:
        idx  = ready[0]
        name = s['sources'][idx].get('name',f'F{idx+1}')
        current_proc = start_ffmpeg(s['source_urls'][idx], name, s.get('source_headers',{}).get(idx,''))

    while not s.get('stop_requested'):
        target = s.get('target_source_idx')
        if target is not None and target != s.get('current_source_idx'):
            url = s['source_urls'].get(target)
            if url and s['source_status'].get(target)=='ready':
                kill_proc(current_proc)
                name = s['sources'][target].get('name',f'F{target+1}')
                current_proc = start_ffmpeg(url, name, s.get('source_headers',{}).get(target,''))
                time.sleep(0.2)
                continue
        if current_proc and current_proc.poll() is not None:
            idx  = s.get('current_source_idx',0)
            name = s['sources'][idx].get('name',f'F{idx+1}') if idx < len(s['sources']) else '?'
            slog(s,'WARN',f'[{oid}] Caido: {name}')
            if idx < len(s['sources']):
                s['source_urls'][idx]   = None
                s['source_status'][idx] = 'fail'
            current_proc = None
            ready = get_ready_sources(s)
            if ready:
                new_idx  = ready[0]
                new_name = s['sources'][new_idx].get('name',f'F{new_idx+1}')
                current_proc = start_ffmpeg(s['source_urls'][new_idx], new_name)
            time.sleep(0.5)
            continue
        if current_proc is None:
            ready = get_ready_sources(s)
            if ready:
                idx  = ready[0]
                name = s['sources'][idx].get('name',f'F{idx+1}')
                current_proc = start_ffmpeg(s['source_urls'][idx], name)
            time.sleep(1)
            continue
        time.sleep(0.5)

    kill_proc(current_proc)
    s['output_procs'].pop(out_idx, None)

def run_stream(sid):
    s = streams.get(sid)
    if not s: return
    n = len(s['sources'])
    s['source_urls']    = {}
    s['source_headers'] = {}
    s['source_status']  = {i:'extracting' for i in range(n)}
    s['output_procs']   = {}
    s['status']         = 'running'
    s['current_source'] = None
    s['started_at']     = datetime.datetime.utcnow().isoformat()
    slog(s,'INFO',f'Iniciando {n} fuentes...')
    for i in range(n):
        threading.Thread(target=extract_worker, args=(sid,i), daemon=True).start()
    threads = []
    for i in range(len(s['outputs'])):
        t = threading.Thread(target=continuous_output, args=(sid,i), daemon=True)
        threads.append(t); t.start()
    for t in threads: t.join()
    s['status']         = 'stopped'
    s['stop_requested'] = False
    s['current_source'] = None
    slog(s,'INFO','Stream detenido')

# ─── COMPUTADORAS ─────────────────────────────────────────────────────────────

def clean_locks(path):
    for lf in ['lock','.parentlock','parent.lock']:
        lp = os.path.join(path, lf)
        try:
            if os.path.islink(lp): os.unlink(lp)
            elif os.path.exists(lp): os.remove(lp)
        except: pass

def make_profile(path):
    os.makedirs(path, exist_ok=True)
    with open(os.path.join(path,'prefs.js'),'w') as f:
        f.write('user_pref("media.autoplay.default", 0);\n')
        f.write('user_pref("media.autoplay.blocking_policy", 0);\n')
        f.write('user_pref("full-screen-api.enabled", true);\n')
        f.write('user_pref("browser.sessionstore.resume_from_crash", false);\n')
        f.write('user_pref("layers.acceleration.disabled", true);\n')
        f.write('user_pref("gfx.webrender.all", false);\n')
        f.write('user_pref("media.av1.enabled", false);\n')
        f.write('user_pref("media.hardware-decode-video.enabled", false);\n')
        f.write('user_pref("media.ffmpeg.vaapi.enabled", false);\n')
        f.write('user_pref("browser.startup.page", 0);\n')
        f.write('user_pref("browser.startup.homepage", "about:blank");\n')
        f.write('user_pref("browser.cache.memory.capacity", 65536);\n')
        f.write('user_pref("browser.cache.disk.enable", false);\n')
        f.write('user_pref("javascript.options.mem.high_water_mark", 128);\n')
        f.write('user_pref("dom.ipc.processCount", 1);\n')
        f.write('user_pref("browser.tabs.remote.autostart", false);\n')
        f.write('user_pref("toolkit.telemetry.enabled", false);\n')
        f.write('user_pref("extensions.pocket.enabled", false);\n')
        f.write('user_pref("media.rdd-process.enabled", false);\n')
        f.write('user_pref("media.rdd-vpx.enabled", false);\n')
        f.write('user_pref("media.ffmpeg.enabled", false);\n')
        f.write('user_pref("media.wmf.enabled", false);\n')
        f.write('user_pref("media.gpu-process-decoder", false);\n')
        f.write('user_pref("media.utility-process.enabled", false);\n')
        f.write('user_pref("dom.ipc.processCount.webIsolated", 1);\n')
        f.write('user_pref("browser.sessionhistory.max_entries", 5);\n')

def get_profile(cid):
    path   = f'/tmp/nexus_profile_{cid}'
    saved  = f'/app/data/saved_profile_{cid}'
    master = '/app/data/master_profile'
    if not os.path.exists(path):
        if os.path.exists(master) and os.listdir(master):
            shutil.copytree(master, path)
            clog_safe(cid, 'Perfil clonado desde master')
        elif os.path.exists(saved) and os.listdir(saved):
            shutil.copytree(saved, path)
        else:
            make_profile(path)
    clean_locks(path)
    if not os.path.exists(master):
        mc = '/app/data/master_cookies.sqlite'
        if os.path.exists(mc):
            try: shutil.copy2(mc, os.path.join(path,'cookies.sqlite'))
            except: pass
    return path

def save_profile(cid):
    c   = computers.get(cid)
    src = f'/tmp/nexus_profile_{cid}'
    dst = f'/app/data/saved_profile_{cid}'
    if not os.path.exists(src): return
    try:
        clean_locks(src)
        if os.path.exists(dst): shutil.rmtree(dst)
        shutil.copytree(src, dst)
        clean_locks(dst)
        if c: clog(c,'Perfil guardado')
    except Exception as e:
        if c: clog(c,f'Error guardando perfil: {e}')

def set_master_profile(cid):
    src = f'/tmp/nexus_profile_{cid}'
    dst = '/app/data/master_profile'
    if not os.path.exists(src): return False,'Perfil no encontrado'
    try:
        clean_locks(src)
        if os.path.exists(dst): shutil.rmtree(dst)
        shutil.copytree(src, dst)
        clean_locks(dst)
        cookies = os.path.join(dst,'cookies.sqlite')
        if os.path.exists(cookies):
            shutil.copy2(cookies,'/app/data/master_cookies.sqlite')
        return True,'Master profile guardado'
    except Exception as e:
        return False, str(e)

def collect_cookies():
    for c in computers.values():
        if c.get('status') != 'running': continue
        src = os.path.join(f'/tmp/nexus_profile_{c["id"]}','cookies.sqlite')
        if os.path.exists(src):
            try: shutil.copy2(src,'/app/data/master_cookies.sqlite'); return True
            except: pass
    return False

def distribute_cookies():
    master = '/app/data/master_cookies.sqlite'
    if not os.path.exists(master): return
    for c in computers.values():
        if c.get('status') != 'running': continue
        dst = os.path.join(f'/tmp/nexus_profile_{c["id"]}','cookies.sqlite')
        try: shutil.copy2(master, dst)
        except: pass

def cookie_loop():
    while True:
        time.sleep(30)
        try: collect_cookies(); distribute_cookies()
        except: pass

threading.Thread(target=cookie_loop, daemon=True).start()

def create_sink(name):
    try:
        subprocess.run(['pactl','load-module','module-null-sink',
            f'sink_name={name}',f'sink_properties=device.description={name}'],
            capture_output=True, timeout=5)
        time.sleep(0.3)
    except: pass

def destroy_sink(name):
    try:
        r = subprocess.run(['pactl','list','modules','short'],
            capture_output=True, text=True, timeout=5)
        for line in r.stdout.split('\n'):
            if name in line:
                subprocess.run(['pactl','unload-module',line.split()[0]],
                    capture_output=True, timeout=3)
                break
    except: pass

def get_sink_inputs():
    try:
        r = subprocess.run(['pactl','list','sink-inputs','short'],
            capture_output=True, text=True, timeout=5)
        return [l.split()[0] for l in r.stdout.strip().split('\n') if l.strip()]
    except: return []

def assign_audio(sink_name, inputs_before, cid):
    c = computers.get(cid)
    for _ in range(25):
        time.sleep(1)
        new_inputs = set(get_sink_inputs()) - inputs_before
        if new_inputs:
            for inp in new_inputs:
                try: subprocess.run(['pactl','move-sink-input',inp,sink_name],capture_output=True,timeout=3)
                except: pass
            if c: clog(c,f'Audio -> {sink_name}')
            return
    if c: clog(c,'Audio: sin nuevo stream detectado')

def start_pulse(dn):
    r = subprocess.run(['pactl','list','sinks','short'],capture_output=True,text=True)
    if r.returncode == 0 and r.stdout.strip(): return
    env = os.environ.copy()
    env['DISPLAY']            = f':{dn}'
    env['XDG_RUNTIME_DIR']    = '/tmp/pulse-runtime'
    env['PULSE_RUNTIME_PATH'] = '/tmp/pulse-runtime'
    os.makedirs('/tmp/pulse-runtime', exist_ok=True)
    subprocess.Popen(['pulseaudio','--start','--exit-idle-time=-1','--daemonize=yes'],
        env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    time.sleep(1.5)

def start_xvfb(dn):
    disp = f':{dn}'
    subprocess.run(['pkill','-9','-f',f'Xvfb {disp}'], capture_output=True)
    time.sleep(0.5)
    for lock in [f'/tmp/.X{dn}-lock',f'/tmp/.X11-unix/X{dn}']:
        try:
            if os.path.exists(lock): os.remove(lock)
        except: pass
    time.sleep(0.3)
    proc = subprocess.Popen(
        ['Xvfb',disp,'-screen','0',f'{WIN_W}x{WIN_H}x16','-ac','-nolisten','tcp'],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(2)
    env = os.environ.copy()
    env['DISPLAY'] = disp
    try: subprocess.run(['xsetroot','-solid','#0a0a0f'],env=env,capture_output=True,timeout=3)
    except: pass
    return proc

def start_wm(dn):
    env = os.environ.copy()
    env['DISPLAY'] = f':{dn}'
    subprocess.Popen(['openbox','--sm-disable'],env=env,
        stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    time.sleep(1)

def start_vnc(cid, dn):
    vnc_port = 5900 + (dn - DISPLAY_BASE)
    ws_port  = 6080 + (dn - DISPLAY_BASE)
    disp     = f':{dn}'
    env      = os.environ.copy()
    env['DISPLAY'] = disp

    # Matar instancias previas
    subprocess.run(['pkill','-9','-f',f'x11vnc.*{disp}'], capture_output=True)
    subprocess.run(['pkill','-9','-f',f'websockify.*{ws_port}'], capture_output=True)
    time.sleep(1)

    # Arrancar x11vnc
    subprocess.Popen(
        ['x11vnc','-display',disp,'-nopw','-listen','localhost',
         '-forever','-shared','-noxdamage','-rfbport',str(vnc_port)],
        env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    # Esperar que x11vnc abra el puerto (hasta 15 segundos)
    vnc_ready = False
    for _ in range(15):
        time.sleep(1)
        # Verificar via conexion TCP directa
        import socket
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(1)
            result = sock.connect_ex(('127.0.0.1', vnc_port))
            sock.close()
            if result == 0:
                vnc_ready = True
                break
        except: pass

    if vnc_ready:
        # Lanzar websockify via shell para que sea independiente
        os.system(f'websockify --web /app/vnc {ws_port} localhost:{vnc_port} '
                  f'> /tmp/ws_{ws_port}.log 2>&1 &')
        time.sleep(2)

    c = computers.get(cid)
    if c:
        c['vnc_port'] = vnc_port
        c['ws_port']  = ws_port
        status = 'VNC listo' if vnc_ready else 'VNC: x11vnc no arranco'
        clog(c, f'{status} en puerto {ws_port}')

def get_windows(dn):
    env = os.environ.copy()
    env['DISPLAY'] = f':{dn}'
    try:
        r = subprocess.run(['wmctrl','-l','-G'],
            env=env, capture_output=True, text=True, timeout=5)
        wins = []
        for line in r.stdout.strip().split('\n'):
            if not line.strip(): continue
            parts = line.split(None, 9)
            if len(parts) < 9: continue
            wid   = parts[0]
            x, y  = int(parts[2]), int(parts[3])
            w, h  = int(parts[4]), int(parts[5])
            title = parts[8] if len(parts) > 8 else ''
            if w < 400 or h < 300 or not title or title == 'N/A': continue
            wins.append({'id':wid,'title':title,'x':x,'y':y,'w':w,'h':h})
        return wins
    except: return []

def open_window(cid, url):
    c = computers.get(cid)
    if not c or c['status'] != 'running': return None
    dn        = c['display_num']
    wid       = str(uuid.uuid4())[:8]
    sink_name = c['sink_name']
    wins_before   = set(w['id'] for w in get_windows(dn))
    inputs_before = set(get_sink_inputs())
    profile = get_profile(cid)
    env = os.environ.copy()
    env['DISPLAY']                     = f':{dn}'
    env['PULSE_SINK']                  = sink_name
    env['MOZ_DISABLE_CONTENT_SANDBOX'] = '1'
    env['MOZ_X11_EGL']                 = '0'
    env['MOZ_DISABLE_RDD_SANDBOX']     = '1'
    firefox_alive = c.get('firefox_proc') and c['firefox_proc'].poll() is None
    if firefox_alive:
        try:
            wins = get_windows(dn)
            if wins:
                fw = wins[0]['id']
                subprocess.run(['xdotool','windowfocus','--sync',fw],env=env,capture_output=True,timeout=3)
                subprocess.run(['xdotool','key','--clearmodifiers','ctrl+t'],env=env,capture_output=True,timeout=3)
                time.sleep(0.8)
                subprocess.run(['xdotool','key','--clearmodifiers','ctrl+l'],env=env,capture_output=True,timeout=3)
                time.sleep(0.4)
                subprocess.run(['xdotool','type','--clearmodifiers','--delay','20',url],env=env,capture_output=True,timeout=10)
                time.sleep(0.2)
                subprocess.run(['xdotool','key','Return'],env=env,capture_output=True,timeout=3)
        except Exception as e:
            clog(c,f'Error abriendo pestana: {e}')
        proc = None
    else:
        cmd  = ['firefox-esr','--profile',profile,'--new-instance',
                '--no-remote','--width',str(WIN_W),'--height',str(WIN_H),url]
        proc = subprocess.Popen(cmd,env=env,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        c['firefox_proc'] = proc
        threading.Thread(target=assign_audio,args=(sink_name,inputs_before,cid),daemon=True).start()
    entry = {'id':wid,'url':url,'pid':proc.pid if proc else 0,
             'proc':proc,'win_id':None,'title':url}
    if 'windows' not in c: c['windows'] = {}
    c['windows'][wid] = entry
    clog(c,f'Abriendo: {url}')
    def detect():
        for _ in range(30):
            time.sleep(1)
            for w in get_windows(dn):
                if w['id'] not in wins_before:
                    entry['win_id'] = w['id']
                    entry['title']  = w['title']
                    clog(c,f'Ventana: "{w["title"][:50]}"')
                    return
        clog(c,'Ventana abierta (sin deteccion automatica)')
    threading.Thread(target=detect,daemon=True).start()
    return wid

def cleanup_computer(cid):
    c = computers.get(cid)
    if not c: return
    for p in list(c.get('rtmp_procs',{}).values()):
        try: p.kill()
        except: pass
    c['rtmp_procs'] = {}
    if c.get('hls_proc'):
        try: c['hls_proc'].kill()
        except: pass
        c['hls_proc'] = None
    if c.get('firefox_proc'):
        try: c['firefox_proc'].kill()
        except: pass
        c['firefox_proc'] = None
    subprocess.run(['pkill','-9','-f',f'nexus_profile_{cid}'],capture_output=True)
    if c.get('sink_name'): destroy_sink(c['sink_name'])
    c['windows'] = {}
    dn = c.get('display_num')
    if dn:
        ws_port = 6080 + (dn - DISPLAY_BASE)
        subprocess.run(['pkill','-9','-f',f'x11vnc.*:{dn}'],capture_output=True)
        subprocess.run(['pkill','-9','-f',f'websockify.*{ws_port}'],capture_output=True)
        if c.get('xvfb_proc'):
            try: c['xvfb_proc'].kill()
            except: pass
        subprocess.run(['pkill','-9','-f',f'Xvfb :{dn}'],capture_output=True)
        c['display_num'] = None
        c['xvfb_proc']   = None
    try: shutil.rmtree(os.path.join(HLS_DIR,cid))
    except: pass

def alloc_display():
    used = {c.get('display_num') for c in computers.values() if c.get('display_num')}
    for i in range(DISPLAY_BASE, DISPLAY_BASE + MAX_COMPUTERS):
        if i not in used: return i
    return None

def run_computer(cid):
    c = computers.get(cid)
    if not c: return
    with display_lock:
        dn = alloc_display()
        if dn is None:
            clog(c,'No hay displays disponibles')
            c['status'] = 'error'
            return
        c['display_num'] = dn
    clog(c,f'Display :{dn} asignado')
    c['xvfb_proc'] = start_xvfb(dn)
    clog(c,'Xvfb listo')
    start_pulse(dn)
    clog(c,'Audio listo')
    sink_name      = f'nexus_sink_{cid}'
    c['sink_name'] = sink_name
    create_sink(sink_name)
    start_wm(dn)
    c['status']       = 'running'
    c['started_at']   = datetime.datetime.utcnow().isoformat()
    c['windows']      = {}
    c['rtmp_procs']   = {}
    c['firefox_proc'] = None
    clog(c,'Computadora lista')
    threading.Thread(target=start_vnc,args=(cid,dn),daemon=True).start()
    start_url = c.get('start_url','')
    if start_url and start_url != 'about:blank':
        time.sleep(2)
        open_window(cid, start_url)
    while not c.get('stop_requested'):
        time.sleep(2)
    save_profile(cid)
    cleanup_computer(cid)
    c['status'] = 'stopped'
    clog(c,'Apagada')

def start_rtmp(cid, oid, win_id=None):
    c   = computers.get(cid)
    out = outputs.get(oid)
    if not c or not out or c['status'] != 'running': return
    dn   = c['display_num']
    disp = f':{dn}'
    dest = out['rtmp'].rstrip('/')
    if out.get('key'): dest += '/' + out['key']
    btr  = out.get('bitrate','3000k')
    abtr = out.get('audio_bitrate','128k')
    fps  = str(out.get('fps',30))
    name = out.get('name',oid)
    env  = os.environ.copy()
    env['DISPLAY'] = disp
    if 'rtmp_procs' not in c: c['rtmp_procs'] = {}
    if oid in c['rtmp_procs']:
        try: c['rtmp_procs'][oid].kill()
        except: pass
        time.sleep(0.5)
    audio_source = c.get('sink_name','default') + '.monitor'
    if win_id:
        try:
            subprocess.run(['xdotool','windowfocus','--sync',win_id],env=env,capture_output=True,timeout=3)
            subprocess.run(['xdotool','windowraise',win_id],env=env,capture_output=True,timeout=3)
            subprocess.run(['xdotool','windowmove','--sync',win_id,'0','0'],env=env,capture_output=True,timeout=3)
            subprocess.run(['xdotool','windowsize','--sync',win_id,str(WIN_W),str(WIN_H)],env=env,capture_output=True,timeout=3)
            time.sleep(0.5)
        except Exception as e:
            clog(c,f'Error preparando ventana: {e}')
    buf     = str(int(btr.replace('k',''))*2)+'k'
    threads = max(1,(os.cpu_count() or 2)-1)
    cmd = [
        'ffmpeg','-y',
        '-f','x11grab','-r','30','-s',f'{WIN_W}x{WIN_H}',
        '-draw_mouse','0','-i',f'{disp}+0,0',
        '-f','pulse','-ac','2','-i',audio_source,
        '-c:v','libx264','-preset','ultrafast','-tune','zerolatency',
        '-threads','2',
        '-b:v',btr,'-maxrate',btr,'-bufsize',buf,
        '-g','60','-sc_threshold','0',
        '-pix_fmt','yuv420p',
        '-c:a','aac','-b:a',abtr,'-ar','44100',
        '-f','flv','-flvflags','no_duration_filesize',dest
    ]
    def do():
        proc = subprocess.Popen(cmd,env=env,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True)
        c['rtmp_procs'][oid] = proc
        clog(c,f'[{name}] RTMP iniciado PID={proc.pid}')
        last_drop = 0
        start_t   = time.time()
        for line in proc.stdout:
            l = line.rstrip()
            if not l: continue
            if 'frame=' in l:
                dm    = re.search(r'drop=\s*(\d+)',l)
                drops = int(dm.group(1)) if dm else 0
                if (time.time()-start_t) > 20 and drops > last_drop:
                    clog(c,f'🔴 [{name}] DROP: {drops-last_drop} frames')
                last_drop = drops
            elif 'error' in l.lower() or 'broken pipe' in l.lower():
                clog(c,f'🔴 [{name}] ERROR: {l}')
        proc.wait()
        clog(c,f'[{name}] RTMP termino rc={proc.returncode}')
        c['rtmp_procs'].pop(oid,None)
    threading.Thread(target=do,daemon=True).start()

# ─── METRICAS ─────────────────────────────────────────────────────────────────

_metrics = {}
_metrics_lock = threading.Lock()

def metrics_worker():
    while True:
        try:
            with open('/proc/stat') as f: cpu1=f.readline().split()
            time.sleep(1)
            with open('/proc/stat') as f: cpu2=f.readline().split()
            idle1=int(cpu1[4]); total1=sum(int(x) for x in cpu1[1:])
            idle2=int(cpu2[4]); total2=sum(int(x) for x in cpu2[1:])
            cpu_pct=round(100*(1-(idle2-idle1)/(total2-total1)),1)
            mem={}
            with open('/proc/meminfo') as f:
                for line in f:
                    k,v=line.split(':')
                    mem[k.strip()]=int(v.strip().split()[0])
            total_mb=mem['MemTotal']//1024
            free_mb=(mem['MemFree']+mem.get('Buffers',0)+mem.get('Cached',0))//1024
            used_mb=total_mb-free_mb
            with _metrics_lock:
                _metrics.update({'cpu':cpu_pct,'ram_used':used_mb,
                    'ram_total':total_mb,'ram_pct':round(used_mb/total_mb*100,1)})
        except: pass
        time.sleep(5)

threading.Thread(target=metrics_worker,daemon=True).start()

# ─── FLASK ────────────────────────────────────────────────────────────────────

@app.after_request
def cors(r):
    r.headers['Access-Control-Allow-Origin']  = '*'
    r.headers['Access-Control-Allow-Methods'] = 'GET,POST,PUT,DELETE,OPTIONS'
    r.headers['Access-Control-Allow-Headers'] = 'Content-Type'
    return r

def J(data, code=200):
    return app.response_class(json.dumps(data,default=str),status=code,mimetype='application/json')

def jreq():
    return request.get_json(force=True,silent=True) or {}

@app.route('/')
def index():
    return open('/app/index.html').read(),200,{'Content-Type':'text/html; charset=utf-8'}

@app.route('/vnc/')
@app.route('/vnc/<path:f>')
def vnc_files(f='vnc.html'):
    return send_from_directory('/app/vnc',f)

@app.route('/hls/<cid>/<path:f>')
def hls_file(cid,f):
    return send_from_directory(os.path.join(HLS_DIR,cid),f)

@app.route('/api/metrics')
def api_metrics():
    with _metrics_lock: return J(_metrics)

# STREAMS
@app.route('/api/streams',methods=['GET','OPTIONS'])
def api_streams_get():
    if request.method=='OPTIONS': return '',204
    result=[]
    for s in streams.values():
        result.append({'id':s['id'],'name':s['name'],'sources':s['sources'],
            'outputs':s['outputs'],'status':s['status'],
            'current_source':s.get('current_source'),
            'current_source_idx':s.get('current_source_idx'),
            'target_source_idx':s.get('target_source_idx'),
            'source_status':{str(k):v for k,v in s.get('source_status',{}).items()},
            'started_at':s.get('started_at'),'logs':s.get('logs',[])[-50:]})
    return J(result)

@app.route('/api/streams',methods=['POST'])
def api_streams_post():
    d=jreq(); sid=str(uuid.uuid4())[:8]
    streams[sid]={'id':sid,'name':d.get('name','Stream'),
        'sources':d.get('sources',[]),'outputs':d.get('outputs',[]),
        'status':'stopped','logs':[],'source_urls':{},'source_status':{},
        'output_procs':{},'stop_requested':False,
        'current_source':None,'current_source_idx':None,
        'target_source_idx':None,'started_at':None}
    save_streams()
    return J({'ok':True,'id':sid})

@app.route('/api/streams/<sid>',methods=['PUT','OPTIONS'])
def api_stream_put(sid):
    if request.method=='OPTIONS': return '',204
    s=streams.get(sid)
    if not s: return J({'error':'no encontrado'},404)
    d=jreq()
    if d.get('name'): s['name']=d['name']
    if d.get('sources') is not None:
        s['sources']=d['sources']
        if s['status']=='running':
            n=len(s['sources'])
            s['source_urls']={}
            s['source_status']={i:'extracting' for i in range(n)}
            for i in range(n):
                threading.Thread(target=extract_worker,args=(sid,i),daemon=True).start()
    if d.get('outputs') is not None: s['outputs']=d['outputs']
    save_streams()
    return J({'ok':True})

@app.route('/api/streams/<sid>/start',methods=['POST','OPTIONS'])
def api_stream_start(sid):
    if request.method=='OPTIONS': return '',204
    s=streams.get(sid)
    if not s: return J({'error':'no encontrado'},404)
    if s['status']=='running': return J({'error':'ya corriendo'})
    if not s['sources']: return J({'error':'sin fuentes'},400)
    if not s['outputs']: return J({'error':'sin salidas'},400)
    s['stop_requested']=False; s['logs']=[]; s['started_at']=datetime.datetime.utcnow().isoformat()
    threading.Thread(target=run_stream,args=(sid,),daemon=True).start()
    return J({'ok':True})

@app.route('/api/streams/<sid>/stop',methods=['POST','OPTIONS'])
def api_stream_stop(sid):
    if request.method=='OPTIONS': return '',204
    s=streams.get(sid)
    if s:
        s['stop_requested']=True
        for p in list(s.get('output_procs',{}).values()):
            try: p.kill()
            except: pass
        s['status']='stopped'; s['current_source']=None
    return J({'ok':True})

@app.route('/api/streams/<sid>/switch',methods=['POST','OPTIONS'])
def api_stream_switch(sid):
    if request.method=='OPTIONS': return '',204
    s=streams.get(sid)
    if not s: return J({'error':'no encontrado'},404)
    idx=int(jreq().get('idx',0))
    if idx<0 or idx>=len(s['sources']): return J({'error':'invalido'},400)
    if s['source_status'].get(idx)!='ready': return J({'error':'fuente no lista'},400)
    s['target_source_idx']=idx
    slog(s,'MANUAL',f'Cambio -> {s["sources"][idx].get("name","F"+str(idx+1))}')
    return J({'ok':True})

@app.route('/api/streams/<sid>',methods=['DELETE','OPTIONS'])
def api_stream_delete(sid):
    if request.method=='OPTIONS': return '',204
    s=streams.pop(sid,None)
    if s:
        s['stop_requested']=True
        for p in list(s.get('output_procs',{}).values()):
            try: p.kill()
            except: pass
    save_streams()
    return J({'ok':True})

# COMPUTADORAS
@app.route('/api/computers',methods=['GET','OPTIONS'])
def api_computers_get():
    if request.method=='OPTIONS': return '',204
    result=[]
    for c in computers.values():
        wins=[]
        if c.get('display_num') and c['status']=='running':
            for rw in get_windows(c['display_num']):
                sw=next((w for w in c.get('windows',{}).values() if w.get('win_id')==rw['id']),None)
                wins.append({'win_id':rw['id'],'title':rw['title'],
                    'x':rw['x'],'y':rw['y'],'w':rw['w'],'h':rw['h'],
                    'url':sw['url'] if sw else '','wid':sw['id'] if sw else None})
        result.append({'id':c['id'],'name':c['name'],'status':c['status'],
            'display_num':c.get('display_num'),'vnc_port':c.get('vnc_port'),
            'ws_port':c.get('ws_port'),'start_url':c.get('start_url',''),
            'started_at':c.get('started_at'),'logs':c.get('logs',[])[-80:],
            'windows':wins,'active_rtmp':list(c.get('rtmp_procs',{}).keys()),
            'hls_active':bool(c.get('hls_proc') and c['hls_proc'].poll() is None)})
    return J(result)

@app.route('/api/computers',methods=['POST'])
def api_computers_post():
    d=jreq(); cid=str(uuid.uuid4())[:8]
    computers[cid]={'id':cid,'name':d.get('name','Computadora'),
        'status':'stopped','start_url':d.get('start_url','about:blank'),
        'display_num':None,'xvfb_proc':None,'firefox_proc':None,
        'vnc_port':None,'ws_port':None,'sink_name':None,
        'windows':{},'rtmp_procs':{},'hls_proc':None,
        'logs':[],'stop_requested':False,'started_at':None}
    return J({'ok':True,'id':cid})

@app.route('/api/computers/<cid>',methods=['DELETE','OPTIONS'])
def api_computer_delete(cid):
    if request.method=='OPTIONS': return '',204
    c=computers.get(cid)
    if c:
        c['stop_requested']=True
        cleanup_computer(cid)
        computers.pop(cid,None)
    return J({'ok':True})

@app.route('/api/computers/<cid>/start',methods=['POST','OPTIONS'])
def api_computer_start(cid):
    if request.method=='OPTIONS': return '',204
    c=computers.get(cid)
    if not c: return J({'error':'No encontrada'},404)
    if c['status']=='running': return J({'error':'Ya activa'})
    if sum(1 for x in computers.values() if x.get('display_num'))>=MAX_COMPUTERS:
        return J({'error':f'Maximo {MAX_COMPUTERS}'},400)
    c['stop_requested']=False
    c['logs']=[f'[{ts()}] Iniciando...']
    threading.Thread(target=run_computer,args=(cid,),daemon=True).start()
    return J({'ok':True})

@app.route('/api/computers/<cid>/stop',methods=['POST','OPTIONS'])
def api_computer_stop(cid):
    if request.method=='OPTIONS': return '',204
    c=computers.get(cid)
    if c: c['stop_requested']=True
    return J({'ok':True})

@app.route('/api/computers/<cid>/open',methods=['POST','OPTIONS'])
def api_computer_open(cid):
    if request.method=='OPTIONS': return '',204
    c=computers.get(cid)
    if not c: return J({'error':'No encontrada'},404)
    if c['status']!='running': return J({'error':'No activa'},400)
    d=jreq(); url=d.get('url','about:blank')
    if not url.startswith('http'): url='https://'+url
    wid=open_window(cid,url)
    return J({'ok':True,'window_id':wid})

@app.route('/api/computers/<cid>/windows/refresh',methods=['POST','OPTIONS'])
def api_windows_refresh(cid):
    if request.method=='OPTIONS': return '',204
    c=computers.get(cid)
    if not c or not c.get('display_num'): return J([])
    return J(get_windows(c['display_num']))

@app.route('/api/computers/<cid>/windows/<win_id>/focus',methods=['POST','OPTIONS'])
def api_window_focus(cid,win_id):
    if request.method=='OPTIONS': return '',204
    c=computers.get(cid)
    if not c or not c.get('display_num'): return J({'error':'No activa'},400)
    env=os.environ.copy()
    env['DISPLAY']=f':{c["display_num"]}'
    try:
        subprocess.run(['xdotool','windowfocus','--sync',win_id],env=env,capture_output=True,timeout=3)
        subprocess.run(['xdotool','windowraise',win_id],env=env,capture_output=True,timeout=3)
        subprocess.run(['xdotool','windowmove','--sync',win_id,'0','0'],env=env,capture_output=True,timeout=3)
        subprocess.run(['xdotool','windowsize','--sync',win_id,str(WIN_W),str(WIN_H)],env=env,capture_output=True,timeout=3)
        clog(c,f'Ventana {win_id} enfocada')
        return J({'ok':True,'ws_port':c.get('ws_port')})
    except Exception as e:
        return J({'error':str(e)},500)

@app.route('/api/computers/<cid>/windows/<wid>/close',methods=['POST','OPTIONS'])
def api_window_close(cid,wid):
    if request.method=='OPTIONS': return '',204
    c=computers.get(cid)
    if not c: return J({'error':'No encontrada'},404)
    w=c.get('windows',{}).get(wid)
    if w:
        win_id=w.get('win_id')
        if win_id:
            env=os.environ.copy()
            env['DISPLAY']=f':{c.get("display_num")}'
            try: subprocess.run(['xdotool','windowclose',win_id],env=env,capture_output=True,timeout=3)
            except: pass
        c['windows'].pop(wid,None)
        clog(c,'Pestana cerrada')
    return J({'ok':True})

@app.route('/api/computers/<cid>/set_master',methods=['POST','OPTIONS'])
def api_set_master(cid):
    if request.method=='OPTIONS': return '',204
    ok,msg=set_master_profile(cid)
    return J({'ok':ok,'msg':msg})

@app.route('/api/computers/<cid>/rtmp/start',methods=['POST','OPTIONS'])
def api_rtmp_start(cid):
    if request.method=='OPTIONS': return '',204
    c=computers.get(cid)
    if not c: return J({'error':'No encontrada'},404)
    if c['status']!='running': return J({'error':'No activa'},400)
    d=jreq(); oid=d.get('output_id'); win_id=d.get('win_id')
    if not oid or oid not in outputs: return J({'error':'Salida no encontrada'},400)
    threading.Thread(target=start_rtmp,args=(cid,oid,win_id),daemon=True).start()
    return J({'ok':True})

@app.route('/api/computers/<cid>/rtmp/stop',methods=['POST','OPTIONS'])
def api_rtmp_stop(cid):
    if request.method=='OPTIONS': return '',204
    c=computers.get(cid)
    if not c: return J({'ok':True})
    oid=jreq().get('output_id')
    if oid and oid in c.get('rtmp_procs',{}):
        try: c['rtmp_procs'][oid].kill()
        except: pass
        c['rtmp_procs'].pop(oid,None)
        clog(c,'RTMP detenido')
    return J({'ok':True})

@app.route('/api/computers/<cid>/hls/start',methods=['POST','OPTIONS'])
def api_hls_start(cid):
    if request.method=='OPTIONS': return '',204
    c=computers.get(cid)
    if not c or c['status']!='running': return J({'error':'No activa'},400)
    if c.get('hls_proc') and c['hls_proc'].poll() is None: return J({'ok':True})
    hls_path=os.path.join(HLS_DIR,cid)
    os.makedirs(hls_path,exist_ok=True)
    dn=c['display_num']
    env=os.environ.copy()
    env['DISPLAY']=f':{dn}'
    cmd=['ffmpeg','-y','-f','x11grab','-r','4','-s',f'{WIN_W}x{WIN_H}','-i',f':{dn}+0,0',
         '-vf','scale=640:360','-c:v','libx264','-preset','ultrafast','-tune','zerolatency',
         '-b:v','300k','-g','8','-f','hls','-hls_time','1','-hls_list_size','4',
         '-hls_flags','delete_segments+omit_endlist',os.path.join(hls_path,'live.m3u8')]
    proc=subprocess.Popen(cmd,env=env,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    c['hls_proc']=proc
    clog(c,f'HLS PID={proc.pid}')
    return J({'ok':True})

@app.route('/api/computers/<cid>/hls/stop',methods=['POST','OPTIONS'])
def api_hls_stop(cid):
    if request.method=='OPTIONS': return '',204
    c=computers.get(cid)
    if c and c.get('hls_proc'):
        try: c['hls_proc'].kill()
        except: pass
        c['hls_proc']=None
    try: shutil.rmtree(os.path.join(HLS_DIR,cid))
    except: pass
    return J({'ok':True})

@app.route('/api/computers/<cid>/save_profile',methods=['POST','OPTIONS'])
def api_save_profile(cid):
    if request.method=='OPTIONS': return '',204
    save_profile(cid)
    return J({'ok':True})

# OUTPUTS
@app.route('/api/outputs',methods=['GET','POST','OPTIONS'])
def api_outputs():
    if request.method=='OPTIONS': return '',204
    if request.method=='GET': return J(list(outputs.values()))
    d=jreq(); oid=str(uuid.uuid4())[:8]
    outputs[oid]={'id':oid,'name':d.get('name','Salida'),
        'rtmp':d.get('rtmp',''),'key':d.get('key',''),
        'bitrate':d.get('bitrate','3000k'),
        'audio_bitrate':d.get('audio_bitrate','128k'),
        'fps':d.get('fps',30)}
    save_outputs()
    return J({'ok':True,'id':oid})

@app.route('/api/outputs/<oid>',methods=['DELETE','OPTIONS'])
def api_output_delete(oid):
    if request.method=='OPTIONS': return '',204
    outputs.pop(oid,None)
    save_outputs()
    return J({'ok':True})

# COOKIES
@app.route('/api/cookies/sync',methods=['POST','OPTIONS'])
def api_cookies_sync():
    if request.method=='OPTIONS': return '',204
    collect_cookies(); distribute_cookies()
    return J({'ok':True,'msg':'Cookies sincronizadas'})

@app.route('/api/cookies/save',methods=['POST','OPTIONS'])
def api_cookies_save():
    if request.method=='OPTIONS': return '',204
    cid=jreq().get('cid')
    if not cid: return J({'error':'cid requerido'},400)
    src=os.path.join(f'/tmp/nexus_profile_{cid}','cookies.sqlite')
    if not os.path.exists(src): return J({'error':'Sin cookies'},400)
    try:
        shutil.copy2(src,'/app/data/master_cookies.sqlite')
        distribute_cookies()
        return J({'ok':True,'msg':'Cookies guardadas'})
    except Exception as e:
        return J({'error':str(e)},500)

@app.route('/api/cookies/status',methods=['GET','OPTIONS'])
def api_cookies_status():
    if request.method=='OPTIONS': return '',204
    master='/app/data/master_cookies.sqlite'
    has=os.path.exists(master)
    size=os.path.getsize(master) if has else 0
    has_mp=os.path.exists('/app/data/master_profile')
    return J({'has_master':has,'size_kb':round(size/1024,1),'has_master_profile':has_mp})

if __name__=='__main__':
    app.run(host='0.0.0.0',port=8080,threaded=True)
