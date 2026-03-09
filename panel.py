from flask import Flask, request, send_from_directory
import subprocess, threading, uuid, json, datetime, time, os, shutil, re

app = Flask(__name__)

computers = {}
outputs   = {}
machine   = {'status':'running','logs':[],'started_at':datetime.datetime.utcnow().isoformat()}

DISPLAY_BASE  = 99
MAX_COMPUTERS = 30
WIN_W, WIN_H  = 1280, 720
HLS_DIR       = '/tmp/nexus_hls'

os.makedirs(HLS_DIR, exist_ok=True)
display_lock = threading.Lock()

def ts():
    return datetime.datetime.utcnow().strftime('%H:%M:%S')

def mlog(msg):
    machine['logs'].append(f'[{ts()}] {msg}')
    if len(machine['logs']) > 200:
        machine['logs'] = machine['logs'][-200:]

def clog(c, msg):
    e = f'[{ts()}] {msg}'
    c['logs'].append(e)
    if len(c['logs']) > 300:
        c['logs'] = c['logs'][-300:]

def alloc_display():
    used = {c.get('display_num') for c in computers.values() if c.get('display_num')}
    for i in range(DISPLAY_BASE, DISPLAY_BASE + MAX_COMPUTERS):
        if i not in used:
            return i
    return None

def clean_locks(path):
    for lf in ['lock', '.parentlock', 'parent.lock']:
        lp = os.path.join(path, lf)
        try:
            if os.path.islink(lp): os.unlink(lp)
            elif os.path.exists(lp): os.remove(lp)
        except: pass

def make_profile(path):
    os.makedirs(path, exist_ok=True)
    with open(os.path.join(path, 'prefs.js'), 'w') as f:
        f.write('user_pref("media.eme.enabled", true);\n')
        f.write('user_pref("media.gmp-widevinecdm.enabled", true);\n')
        f.write('user_pref("media.gmp-widevinecdm.visible", true);\n')
        f.write('user_pref("media.autoplay.default", 0);\n')
        f.write('user_pref("media.autoplay.blocking_policy", 0);\n')
        f.write('user_pref("full-screen-api.enabled", true);\n')
        f.write('user_pref("browser.sessionstore.resume_from_crash", false);\n')
        f.write('user_pref("privacy.resistFingerprinting", false);\n')
        f.write('user_pref("layers.acceleration.disabled", true);\n')
        f.write('user_pref("gfx.webrender.all", false);\n')
        f.write('user_pref("media.av1.enabled", false);\n')
        f.write('user_pref("media.hardware-decode-video.enabled", false);\n')
        f.write('user_pref("media.ffmpeg.vaapi.enabled", false);\n')
        f.write('user_pref("network.http.max-connections", 900);\n')
        f.write('user_pref("network.http.max-persistent-connections-per-server", 20);\n')
        f.write('user_pref("browser.startup.page", 0);\n')
        f.write('user_pref("browser.startup.homepage", "about:blank");\n')

def get_profile(cid):
    path  = f'/tmp/nexus_profile_{cid}'
    saved = f'/app/saved_profile_{cid}'
    master_profile = '/app/master_profile'
    if not os.path.exists(path):
        # Primero intentar clonar desde master_profile completo
        if os.path.exists(master_profile) and os.listdir(master_profile):
            shutil.copytree(master_profile, path)
            clog_safe(cid, 'Perfil clonado desde master')
        elif os.path.exists(saved) and os.listdir(saved):
            shutil.copytree(saved, path)
        else:
            make_profile(path)
    clean_locks(path)
    # Solo copiar cookies si no hay master_profile
    if not os.path.exists(master_profile):
        master = '/app/master_cookies.sqlite'
        if os.path.exists(master):
            try: shutil.copy2(master, os.path.join(path, 'cookies.sqlite'))
            except: pass
    return path

def clog_safe(cid, msg):
    c = computers.get(cid)
    if c:
        clog(c, msg)

def save_profile(cid):
    c   = computers.get(cid)
    src = f'/tmp/nexus_profile_{cid}'
    dst = f'/app/saved_profile_{cid}'
    if not os.path.exists(src): return
    try:
        clean_locks(src)
        for fn in ['sessionstore.jsonlz4', 'lock', '.parentlock']:
            fp = os.path.join(src, fn)
            try:
                if os.path.islink(fp): os.unlink(fp)
                elif os.path.exists(fp): os.remove(fp)
            except: pass
        if os.path.exists(dst): shutil.rmtree(dst)
        shutil.copytree(src, dst)
        clean_locks(dst)
        if c: clog(c, 'Perfil guardado')
    except Exception as e:
        if c: clog(c, f'Error guardando perfil: {e}')

def set_master_profile(cid):
    """Clonar perfil de una computadora como master para todas las demas"""
    src = f'/tmp/nexus_profile_{cid}'
    dst = '/app/master_profile'
    if not os.path.exists(src):
        return False, 'Perfil no encontrado'
    try:
        clean_locks(src)
        if os.path.exists(dst): shutil.rmtree(dst)
        shutil.copytree(src, dst)
        clean_locks(dst)
        # Tambien actualizar master_cookies
        cookies = os.path.join(dst, 'cookies.sqlite')
        if os.path.exists(cookies):
            shutil.copy2(cookies, '/app/master_cookies.sqlite')
        return True, 'Master profile guardado'
    except Exception as e:
        return False, str(e)

def collect_cookies():
    for c in computers.values():
        if c.get('status') != 'running': continue
        src = os.path.join(f'/tmp/nexus_profile_{c["id"]}', 'cookies.sqlite')
        if os.path.exists(src):
            try:
                shutil.copy2(src, '/app/master_cookies.sqlite')
                return True
            except: pass
    return False

def distribute_cookies():
    master = '/app/master_cookies.sqlite'
    if not os.path.exists(master): return
    for c in computers.values():
        if c.get('status') != 'running': continue
        dst = os.path.join(f'/tmp/nexus_profile_{c["id"]}', 'cookies.sqlite')
        try: shutil.copy2(master, dst)
        except: pass

def cookie_loop():
    while True:
        time.sleep(30)
        try:
            collect_cookies()
            distribute_cookies()
        except: pass

threading.Thread(target=cookie_loop, daemon=True).start()

def create_sink(name):
    try:
        subprocess.run(['pactl', 'load-module', 'module-null-sink',
            f'sink_name={name}',
            f'sink_properties=device.description={name}'],
            capture_output=True, timeout=5)
        time.sleep(0.3)
    except: pass

def destroy_sink(name):
    try:
        r = subprocess.run(['pactl', 'list', 'modules', 'short'],
            capture_output=True, text=True, timeout=5)
        for line in r.stdout.split('\n'):
            if name in line:
                mod_id = line.split()[0]
                subprocess.run(['pactl', 'unload-module', mod_id],
                    capture_output=True, timeout=3)
                break
    except: pass

def get_sink_inputs():
    try:
        r = subprocess.run(['pactl', 'list', 'sink-inputs', 'short'],
            capture_output=True, text=True, timeout=5)
        return [l.split()[0] for l in r.stdout.strip().split('\n') if l.strip()]
    except: return []

def assign_audio(sink_name, inputs_before, cid):
    c = computers.get(cid)
    for _ in range(25):
        time.sleep(1)
        inputs_now = set(get_sink_inputs())
        new_inputs = inputs_now - inputs_before
        if new_inputs:
            for inp in new_inputs:
                try:
                    subprocess.run(['pactl', 'move-sink-input', inp, sink_name],
                        capture_output=True, timeout=3)
                except: pass
            if c: clog(c, f'Audio -> {sink_name}')
            return
    if c: clog(c, 'Audio: sin nuevo stream detectado')

def start_xvfb(dn):
    disp = f':{dn}'
    subprocess.run(['pkill', '-9', '-f', f'Xvfb {disp}'], capture_output=True)
    time.sleep(0.5)
    for lock in [f'/tmp/.X{dn}-lock', f'/tmp/.X11-unix/X{dn}']:
        try:
            if os.path.exists(lock): os.remove(lock)
        except: pass
    time.sleep(0.3)
    proc = subprocess.Popen(
        ['Xvfb', disp, '-screen', '0', f'{WIN_W}x{WIN_H}x24', '-ac'],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT
    )
    time.sleep(2)
    env = os.environ.copy()
    env['DISPLAY'] = disp
    try: subprocess.run(['xsetroot', '-solid', '#0a0a0f'], env=env, capture_output=True, timeout=3)
    except: pass
    return proc

def start_pulse(dn):
    # Verificar si pulseaudio ya corre — si corre, no hacer nada
    r = subprocess.run(['pactl', 'list', 'sinks', 'short'],
        capture_output=True, text=True)
    if r.returncode == 0 and r.stdout.strip():
        return  # Ya esta corriendo, no matar
    # Solo arrancar si no hay pulseaudio activo
    env = os.environ.copy()
    env['DISPLAY'] = f':{dn}'
    env['XDG_RUNTIME_DIR'] = '/tmp/pulse-runtime'
    env['PULSE_RUNTIME_PATH'] = '/tmp/pulse-runtime'
    os.makedirs('/tmp/pulse-runtime', exist_ok=True)
    subprocess.Popen(
        ['pulseaudio', '--start', '--exit-idle-time=-1', '--daemonize=yes'],
        env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    time.sleep(1.5)

def start_wm(dn):
    env = os.environ.copy()
    env['DISPLAY'] = f':{dn}'
    subprocess.Popen(['openbox', '--sm-disable'], env=env,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    time.sleep(1)

def start_vnc(cid, dn):
    vnc_port = 5900 + (dn - DISPLAY_BASE)
    ws_port  = 6080 + (dn - DISPLAY_BASE)
    disp     = f':{dn}'
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
    c = computers.get(cid)
    if c:
        c['vnc_port'] = vnc_port
        c['ws_port']  = ws_port
        clog(c, f'VNC listo en puerto {ws_port}')

def get_windows(dn):
    env = os.environ.copy()
    env['DISPLAY'] = f':{dn}'
    try:
        r = subprocess.run(['wmctrl', '-l', '-G'],
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
            if w < 400 or h < 300: continue
            if not title or title == 'N/A': continue
            wins.append({'id': wid, 'title': title, 'x': x, 'y': y, 'w': w, 'h': h})
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
                subprocess.run(['xdotool', 'windowfocus', '--sync', fw], env=env, capture_output=True, timeout=3)
                subprocess.run(['xdotool', 'key', '--clearmodifiers', 'ctrl+t'], env=env, capture_output=True, timeout=3)
                time.sleep(0.8)
                subprocess.run(['xdotool', 'key', '--clearmodifiers', 'ctrl+l'], env=env, capture_output=True, timeout=3)
                time.sleep(0.4)
                subprocess.run(['xdotool', 'type', '--clearmodifiers', '--delay', '20', url], env=env, capture_output=True, timeout=10)
                time.sleep(0.2)
                subprocess.run(['xdotool', 'key', 'Return'], env=env, capture_output=True, timeout=3)
        except Exception as e:
            clog(c, f'Error abriendo pestana: {e}')
        proc = None
    else:
        cmd  = ['firefox-esr', '--profile', profile, '--new-instance',
                '--no-remote', '--width', str(WIN_W), '--height', str(WIN_H), url]
        proc = subprocess.Popen(cmd, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        c['firefox_proc'] = proc
        threading.Thread(target=assign_audio, args=(sink_name, inputs_before, cid), daemon=True).start()
    entry = {'id': wid, 'url': url, 'pid': proc.pid if proc else 0,
             'proc': proc, 'win_id': None, 'title': url}
    if 'windows' not in c: c['windows'] = {}
    c['windows'][wid] = entry
    clog(c, f'Abriendo: {url}')
    def detect():
        for _ in range(30):
            time.sleep(1)
            for w in get_windows(dn):
                if w['id'] not in wins_before:
                    entry['win_id'] = w['id']
                    entry['title']  = w['title']
                    clog(c, f'Ventana: "{w["title"][:50]}"')
                    return
        clog(c, 'Ventana abierta (sin deteccion automatica)')
    threading.Thread(target=detect, daemon=True).start()
    return wid

def cleanup_computer(cid):
    c = computers.get(cid)
    if not c: return
    for p in list(c.get('rtmp_procs', {}).values()):
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
    subprocess.run(['pkill', '-9', '-f', f'nexus_profile_{cid}'], capture_output=True)
    if c.get('sink_name'):
        destroy_sink(c['sink_name'])
    c['windows'] = {}
    dn = c.get('display_num')
    if dn:
        ws_port = 6080 + (dn - DISPLAY_BASE)
        subprocess.run(['pkill', '-f', f'x11vnc.*:{dn}'], capture_output=True)
        subprocess.run(['pkill', '-f', f'websockify.*{ws_port}'], capture_output=True)
        subprocess.run(['pkill', '-9', '-f', 'pulseaudio'], capture_output=True)
        subprocess.run(['pkill', '-f', 'openbox'], capture_output=True)
        if c.get('xvfb_proc'):
            try: c['xvfb_proc'].kill()
            except: pass
        subprocess.run(['pkill', '-9', '-f', f'Xvfb :{dn}'], capture_output=True)
        c['display_num'] = None
        c['xvfb_proc']   = None
    try: shutil.rmtree(os.path.join(HLS_DIR, cid))
    except: pass

def run_computer(cid):
    c = computers.get(cid)
    if not c: return
    with display_lock:
        dn = alloc_display()
        if dn is None:
            clog(c, 'No hay displays disponibles')
            c['status'] = 'error'
            return
        c['display_num'] = dn
    clog(c, f'Display :{dn} asignado')
    c['xvfb_proc'] = start_xvfb(dn)
    clog(c, 'Xvfb listo')
    start_pulse(dn)
    clog(c, 'Audio listo')
    sink_name      = f'nexus_sink_{cid}'
    c['sink_name'] = sink_name
    create_sink(sink_name)
    start_wm(dn)
    c['status']       = 'running'
    c['started_at']   = datetime.datetime.utcnow().isoformat()
    c['windows']      = {}
    c['rtmp_procs']   = {}
    c['firefox_proc'] = None
    clog(c, 'Computadora lista')
    threading.Thread(target=start_vnc, args=(cid, dn), daemon=True).start()
    start_url = c.get('start_url', '')
    if start_url and start_url != 'about:blank':
        time.sleep(2)
        open_window(cid, start_url)
    while not c.get('stop_requested'):
        time.sleep(2)
    save_profile(cid)
    cleanup_computer(cid)
    c['status'] = 'stopped'
    clog(c, 'Apagada')

def start_rtmp(cid, oid, win_id=None):
    c   = computers.get(cid)
    out = outputs.get(oid)
    if not c or not out or c['status'] != 'running': return
    dn   = c['display_num']
    disp = f':{dn}'
    dest = out['rtmp'].rstrip('/')
    if out.get('key'): dest += '/' + out['key']
    btr  = out.get('bitrate', '3000k')
    abtr = out.get('audio_bitrate', '128k')
    fps  = str(out.get('fps', 30))
    name = out.get('name', oid)
    env  = os.environ.copy()
    env['DISPLAY'] = disp
    if 'rtmp_procs' not in c: c['rtmp_procs'] = {}
    if oid in c['rtmp_procs']:
        try: c['rtmp_procs'][oid].kill()
        except: pass
        time.sleep(0.5)
    audio_source = c.get('sink_name', 'default') + '.monitor'
    if win_id:
        try:
            subprocess.run(['xdotool', 'windowfocus', '--sync', win_id], env=env, capture_output=True, timeout=3)
            subprocess.run(['xdotool', 'windowraise', win_id], env=env, capture_output=True, timeout=3)
            subprocess.run(['xdotool', 'windowmove', '--sync', win_id, '0', '0'], env=env, capture_output=True, timeout=3)
            subprocess.run(['xdotool', 'windowsize', '--sync', win_id, str(WIN_W), str(WIN_H)], env=env, capture_output=True, timeout=3)
            time.sleep(0.5)
        except Exception as e:
            clog(c, f'Error preparando ventana: {e}')
    buf       = str(int(btr.replace('k', '')) * 2) + 'k'
    cpu_count = os.cpu_count() or 2
    threads   = max(1, cpu_count - 1)
    cmd = [
        'ffmpeg', '-y',
        '-f', 'x11grab', '-r', fps, '-s', f'{WIN_W}x{WIN_H}',
        '-draw_mouse', '0',
        '-i', f'{disp}+0,0',
        '-f', 'pulse', '-ac', '2', '-i', audio_source,
        '-c:v', 'libx264', '-preset', 'veryfast',
        '-tune', 'zerolatency',
        '-threads', str(threads),
        '-b:v', btr, '-maxrate', btr, '-bufsize', buf,
        '-g', str(int(fps) * 2),
        '-sc_threshold', '0',
        '-pix_fmt', 'yuv420p',
        '-c:a', 'aac', '-b:a', abtr, '-ar', '44100',
        '-f', 'flv',
        '-flvflags', 'no_duration_filesize',
        dest
    ]
    def do():
        proc = subprocess.Popen(cmd, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        c['rtmp_procs'][oid] = proc
        clog(c, f'[{name}] RTMP iniciado PID={proc.pid}')
        last_drop_count = 0
        last_fps_warn   = 0
        stream_start    = time.time()
        for line in proc.stdout:
            l = line.rstrip()
            if not l: continue
            if 'frame=' in l:
                fps_match  = re.search(r'fps=\s*(\d+\.?\d*)', l)
                drop_match = re.search(r'drop=\s*(\d+)', l)
                dup_match  = re.search(r'dup=\s*(\d+)', l)
                real_fps   = float(fps_match.group(1))  if fps_match  else None
                drops      = int(drop_match.group(1))   if drop_match else 0
                dups       = int(dup_match.group(1))    if dup_match  else 0
                warmup_ok  = (time.time() - stream_start) > 20
                target_fps = float(fps)
                if warmup_ok and real_fps is not None and real_fps < target_fps * 0.8:
                    now = time.time()
                    if now - last_fps_warn > 10:
                        last_fps_warn = now
                        clog(c, f'⚠️ [{name}] FPS BAJO: {real_fps:.1f}/{target_fps:.0f}')
                if warmup_ok and drops > last_drop_count:
                    nuevos = drops - last_drop_count
                    last_drop_count = drops
                    clog(c, f'🔴 [{name}] DROP: {nuevos} frame(s) perdido(s) (total={drops})')
                elif drops > last_drop_count:
                    last_drop_count = drops
            elif 'error' in l.lower() or 'broken pipe' in l.lower():
                clog(c, f'🔴 [{name}] ERROR: {l}')
        proc.wait()
        clog(c, f'[{name}] RTMP termino rc={proc.returncode}')
        c['rtmp_procs'].pop(oid, None)
    threading.Thread(target=do, daemon=True).start()
    # Arrancar rotacion automatica cada 4 horas
    threading.Thread(target=rotation_loop, args=(cid, oid, 1.5), daemon=True).start()


def make_reconnect_frame(dn):
    """Genera una imagen de 'Reconectando...' usando ffmpeg"""
    env = os.environ.copy()
    env['DISPLAY'] = f':{dn}'
    out = f'/tmp/reconectando_{dn}.png'
    cmd = [
        'ffmpeg', '-y',
        '-f', 'lavfi',
        '-i', f'color=c=black:size=1280x720:rate=30',
        '-vf', "drawtext=fontsize=48:fontcolor=white:x=(w-text_w)/2:y=(h-text_h)/2-40:text='Espere un momento...',drawtext=fontsize=32:fontcolor=yellow:x=(w-text_w)/2:y=(h-text_h)/2+30:text='Estamos reconectando'",
        '-frames:v', '1', out
    ]
    subprocess.run(cmd, capture_output=True, env=env)
    return out

def rotation_loop(cid, oid, interval_hours=4):
    """Cada X horas pausa el stream 4 segundos con cartel y lo reanuda"""
    c = computers.get(cid)
    if not c: return
    interval = interval_hours * 3600
    time.sleep(interval)
    while True:
        try:
            c = computers.get(cid)
            if not c or c.get('stop_requested'): break
            if oid not in c.get('rtmp_procs', {}): break

            out = outputs.get(oid)
            if not out: break

            clog(c, f'[{out.get("name",oid)}] 🔄 Rotacion automatica iniciando...')

            # Matar ffmpeg actual
            proc = c['rtmp_procs'].get(oid)
            if proc:
                try: proc.kill()
                except: pass
            c['rtmp_procs'].pop(oid, None)
            time.sleep(0.5)

            # Enviar cartel "Reconectando" al RTMP por 5 segundos
            dn   = c.get('display_num')
            dest = out['rtmp'].rstrip('/')
            if out.get('key'): dest += '/' + out['key']
            btr  = out.get('bitrate', '3000k')
            abtr = out.get('audio_bitrate', '128k')
            fps  = str(out.get('fps', 30))
            env  = os.environ.copy()
            env['DISPLAY'] = f':{dn}'

            cartel_cmd = [
                'ffmpeg', '-y',
                '-f', 'lavfi',
                '-i', f'color=c=black:size={WIN_W}x{WIN_H}:rate={fps}',
                '-f', 'lavfi', '-i', 'anullsrc=r=44100:cl=stereo',
                '-vf', "drawtext=fontfile=/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf:fontsize=52:fontcolor=white:x=(w-text_w)/2:y=(h-text_h)/2-50:text='Por favor espere...',drawtext=fontfile=/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf:fontsize=36:fontcolor=yellow:x=(w-text_w)/2:y=(h-text_h)/2+20:text='Estamos reconectando'",
                '-c:v', 'libx264', '-preset', 'ultrafast',
                '-b:v', btr, '-pix_fmt', 'yuv420p',
                '-c:a', 'aac', '-b:a', abtr, '-ar', '44100',
                '-t', '5',
                '-f', 'flv',
                '-flvflags', 'no_duration_filesize',
                dest
            ]
            cartel_proc = subprocess.Popen(cartel_cmd, env=env,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
            cartel_proc.wait()
            clog(c, f'[{out.get("name",oid)}] Cartel enviado, reiniciando stream...')

            # Reiniciar stream normal
            time.sleep(0.5)
            start_rtmp(cid, oid)
            clog(c, f'[{out.get("name",oid)}] ✅ Stream reconectado')

        except Exception as e:
            clog(c, f'Error en rotacion: {e}')

        time.sleep(interval)

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

@app.route('/hls/<cid>/<path:f>')
def hls_file(cid, f):
    return send_from_directory(os.path.join(HLS_DIR, cid), f)

@app.route('/api/machine', methods=['GET','OPTIONS'])
def api_machine():
    if request.method == 'OPTIONS': return '', 204
    return J({'status': machine['status'], 'logs': machine['logs'][-60:],
              'computers_active': sum(1 for c in computers.values() if c['status']=='running'),
              'computers_max': MAX_COMPUTERS, 'started_at': machine['started_at']})

@app.route('/api/computers', methods=['GET','OPTIONS'])
def api_computers_get():
    if request.method == 'OPTIONS': return '', 204
    result = []
    for c in computers.values():
        wins = []
        if c.get('display_num') and c['status'] == 'running':
            for rw in get_windows(c['display_num']):
                sw = next((w for w in c.get('windows', {}).values() if w.get('win_id') == rw['id']), None)
                wins.append({
                    'win_id': rw['id'], 'title': rw['title'],
                    'x': rw['x'], 'y': rw['y'], 'w': rw['w'], 'h': rw['h'],
                    'url': sw['url'] if sw else '',
                    'wid': sw['id']  if sw else None,
                })
        result.append({
            'id': c['id'], 'name': c['name'], 'status': c['status'],
            'display_num': c.get('display_num'), 'vnc_port': c.get('vnc_port'),
            'ws_port': c.get('ws_port'), 'start_url': c.get('start_url', ''),
            'started_at': c.get('started_at'), 'logs': c.get('logs', [])[-80:],
            'windows': wins, 'active_rtmp': list(c.get('rtmp_procs', {}).keys()),
            'hls_active': bool(c.get('hls_proc') and c['hls_proc'].poll() is None),
            'sink_name': c.get('sink_name', ''),
        })
    return J(result)

@app.route('/api/computers', methods=['POST'])
def api_computers_post():
    d   = jreq()
    cid = str(uuid.uuid4())[:8]
    computers[cid] = {
        'id': cid, 'name': d.get('name', 'Computadora'),
        'status': 'stopped', 'start_url': d.get('start_url', 'about:blank'),
        'display_num': None, 'xvfb_proc': None, 'firefox_proc': None,
        'vnc_port': None, 'ws_port': None, 'sink_name': None,
        'windows': {}, 'rtmp_procs': {}, 'hls_proc': None,
        'logs': [], 'stop_requested': False, 'started_at': None
    }
    return J({'ok': True, 'id': cid})

@app.route('/api/computers/<cid>', methods=['DELETE','OPTIONS'])
def api_computer_delete(cid):
    if request.method == 'OPTIONS': return '', 204
    c = computers.get(cid)
    if c:
        c['stop_requested'] = True
        cleanup_computer(cid)
        computers.pop(cid, None)
    return J({'ok': True})

@app.route('/api/computers/<cid>/start', methods=['POST','OPTIONS'])
def api_computer_start(cid):
    if request.method == 'OPTIONS': return '', 204
    c = computers.get(cid)
    if not c: return J({'error': 'No encontrada'}, 404)
    if c['status'] == 'running': return J({'error': 'Ya activa'})
    active = sum(1 for x in computers.values() if x.get('display_num'))
    if active >= MAX_COMPUTERS: return J({'error': f'Maximo {MAX_COMPUTERS} computadoras'}, 400)
    c['stop_requested'] = False
    c['logs'] = [f'[{ts()}] Iniciando...']
    threading.Thread(target=run_computer, args=(cid,), daemon=True).start()
    return J({'ok': True})

@app.route('/api/computers/<cid>/stop', methods=['POST','OPTIONS'])
def api_computer_stop(cid):
    if request.method == 'OPTIONS': return '', 204
    c = computers.get(cid)
    if not c: return J({'ok': True})
    c['stop_requested'] = True
    return J({'ok': True})

@app.route('/api/computers/<cid>/open', methods=['POST','OPTIONS'])
def api_computer_open(cid):
    if request.method == 'OPTIONS': return '', 204
    c = computers.get(cid)
    if not c: return J({'error': 'No encontrada'}, 404)
    if c['status'] != 'running': return J({'error': 'No activa'}, 400)
    d   = jreq()
    url = d.get('url', 'about:blank')
    if not url.startswith('http'): url = 'https://' + url
    wid = open_window(cid, url)
    return J({'ok': True, 'window_id': wid})

@app.route('/api/computers/<cid>/windows/refresh', methods=['POST','OPTIONS'])
def api_windows_refresh(cid):
    if request.method == 'OPTIONS': return '', 204
    c = computers.get(cid)
    if not c or not c.get('display_num'): return J([])
    return J(get_windows(c['display_num']))

@app.route('/api/computers/<cid>/windows/<win_id>/focus', methods=['POST','OPTIONS'])
def api_window_focus(cid, win_id):
    if request.method == 'OPTIONS': return '', 204
    c = computers.get(cid)
    if not c or not c.get('display_num'): return J({'error': 'No activa'}, 400)
    dn  = c['display_num']
    env = os.environ.copy()
    env['DISPLAY'] = f':{dn}'
    try:
        subprocess.run(['xdotool', 'windowfocus', '--sync', win_id], env=env, capture_output=True, timeout=3)
        subprocess.run(['xdotool', 'windowraise', win_id], env=env, capture_output=True, timeout=3)
        subprocess.run(['xdotool', 'windowmove', '--sync', win_id, '0', '0'], env=env, capture_output=True, timeout=3)
        subprocess.run(['xdotool', 'windowsize', '--sync', win_id, str(WIN_W), str(WIN_H)], env=env, capture_output=True, timeout=3)
        clog(c, f'Ventana {win_id} enfocada')
        return J({'ok': True, 'ws_port': c.get('ws_port')})
    except Exception as e:
        return J({'error': str(e)}, 500)

@app.route('/api/computers/<cid>/windows/<wid>/close', methods=['POST','OPTIONS'])
def api_window_close(cid, wid):
    if request.method == 'OPTIONS': return '', 204
    c = computers.get(cid)
    if not c: return J({'error': 'No encontrada'}, 404)
    w = c.get('windows', {}).get(wid)
    if w:
        win_id = w.get('win_id')
        if win_id:
            env = os.environ.copy()
            env['DISPLAY'] = f':{c.get("display_num")}'
            try: subprocess.run(['xdotool', 'windowclose', win_id], env=env, capture_output=True, timeout=3)
            except: pass
        c['windows'].pop(wid, None)
        clog(c, 'Pestana cerrada')
    return J({'ok': True})

@app.route('/api/computers/<cid>/save_profile', methods=['POST','OPTIONS'])
def api_save_profile(cid):
    if request.method == 'OPTIONS': return '', 204
    save_profile(cid)
    return J({'ok': True, 'msg': 'Perfil guardado'})

@app.route('/api/computers/<cid>/set_master', methods=['POST','OPTIONS'])
def api_set_master(cid):
    if request.method == 'OPTIONS': return '', 204
    ok, msg = set_master_profile(cid)
    return J({'ok': ok, 'msg': msg})

@app.route('/api/computers/<cid>/rtmp/start', methods=['POST','OPTIONS'])
def api_rtmp_start(cid):
    if request.method == 'OPTIONS': return '', 204
    c = computers.get(cid)
    if not c: return J({'error': 'No encontrada'}, 404)
    if c['status'] != 'running': return J({'error': 'No activa'}, 400)
    d      = jreq()
    oid    = d.get('output_id')
    win_id = d.get('win_id')
    if not oid or oid not in outputs: return J({'error': 'Salida no encontrada'}, 400)
    threading.Thread(target=start_rtmp, args=(cid, oid, win_id), daemon=True).start()
    return J({'ok': True})

@app.route('/api/computers/<cid>/rtmp/stop', methods=['POST','OPTIONS'])
def api_rtmp_stop(cid):
    if request.method == 'OPTIONS': return '', 204
    c = computers.get(cid)
    if not c: return J({'ok': True})
    oid = jreq().get('output_id')
    if oid and oid in c.get('rtmp_procs', {}):
        try: c['rtmp_procs'][oid].kill()
        except: pass
        c['rtmp_procs'].pop(oid, None)
        clog(c, 'RTMP detenido')
    return J({'ok': True})

@app.route('/api/computers/<cid>/hls/start', methods=['POST','OPTIONS'])
def api_hls_start(cid):
    if request.method == 'OPTIONS': return '', 204
    c = computers.get(cid)
    if not c or c['status'] != 'running': return J({'error': 'No activa'}, 400)
    if c.get('hls_proc') and c['hls_proc'].poll() is None: return J({'ok': True})
    hls_path = os.path.join(HLS_DIR, cid)
    os.makedirs(hls_path, exist_ok=True)
    dn  = c['display_num']
    env = os.environ.copy()
    env['DISPLAY'] = f':{dn}'
    cmd = ['ffmpeg', '-y',
           '-f', 'x11grab', '-r', '4', '-s', f'{WIN_W}x{WIN_H}', '-i', f':{dn}+0,0',
           '-vf', 'scale=640:360',
           '-c:v', 'libx264', '-preset', 'ultrafast', '-tune', 'zerolatency',
           '-b:v', '300k', '-g', '8',
           '-f', 'hls', '-hls_time', '1', '-hls_list_size', '4',
           '-hls_flags', 'delete_segments+omit_endlist',
           os.path.join(hls_path, 'live.m3u8')]
    proc = subprocess.Popen(cmd, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    c['hls_proc'] = proc
    clog(c, f'HLS PID={proc.pid}')
    return J({'ok': True})

@app.route('/api/computers/<cid>/hls/stop', methods=['POST','OPTIONS'])
def api_hls_stop(cid):
    if request.method == 'OPTIONS': return '', 204
    c = computers.get(cid)
    if c and c.get('hls_proc'):
        try: c['hls_proc'].kill()
        except: pass
        c['hls_proc'] = None
    try: shutil.rmtree(os.path.join(HLS_DIR, cid))
    except: pass
    return J({'ok': True})

@app.route('/api/outputs', methods=['GET','POST','OPTIONS'])
def api_outputs():
    if request.method == 'OPTIONS': return '', 204
    if request.method == 'GET': return J(list(outputs.values()))
    d   = jreq()
    oid = str(uuid.uuid4())[:8]
    outputs[oid] = {
        'id': oid, 'name': d.get('name', 'Salida'),
        'rtmp': d.get('rtmp', ''), 'key': d.get('key', ''),
        'bitrate': d.get('bitrate', '3000k'),
        'audio_bitrate': d.get('audio_bitrate', '128k'),
        'fps': d.get('fps', 30),
        'created_at': datetime.datetime.utcnow().isoformat()
    }
    return J({'ok': True, 'id': oid})

@app.route('/api/outputs/<oid>', methods=['PUT','DELETE','OPTIONS'])
def api_output(oid):
    if request.method == 'OPTIONS': return '', 204
    if request.method == 'DELETE':
        outputs.pop(oid, None)
        return J({'ok': True})
    o = outputs.get(oid)
    if not o: return J({'error': 'No encontrada'}, 404)
    o.update(jreq())
    return J({'ok': True})

@app.route('/api/cookies/sync', methods=['POST','OPTIONS'])
def api_cookies_sync():
    if request.method == 'OPTIONS': return '', 204
    collect_cookies()
    distribute_cookies()
    return J({'ok': True, 'msg': 'Cookies sincronizadas'})

@app.route('/api/cookies/save', methods=['POST','OPTIONS'])
def api_cookies_save():
    if request.method == 'OPTIONS': return '', 204
    d   = jreq()
    cid = d.get('cid')
    if not cid: return J({'error': 'cid requerido'}, 400)
    src = os.path.join(f'/tmp/nexus_profile_{cid}', 'cookies.sqlite')
    if not os.path.exists(src): return J({'error': 'Sin cookies aun'}, 400)
    try:
        shutil.copy2(src, '/app/master_cookies.sqlite')
        distribute_cookies()
        return J({'ok': True, 'msg': 'Cookies guardadas y distribuidas'})
    except Exception as e:
        return J({'error': str(e)}, 500)

@app.route('/api/cookies/status', methods=['GET','OPTIONS'])
def api_cookies_status():
    if request.method == 'OPTIONS': return '', 204
    master = '/app/master_cookies.sqlite'
    has    = os.path.exists(master)
    size   = os.path.getsize(master) if has else 0
    master_profile = '/app/master_profile'
    has_mp = os.path.exists(master_profile)
    return J({'has_master': has, 'size_kb': round(size/1024, 1),
              'has_master_profile': has_mp})

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=8080, threaded=True)
# PATCH - sobrescribir start_pulse para no matar pulseaudio global
