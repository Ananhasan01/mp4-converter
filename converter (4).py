#!/usr/bin/env python3
"""
MKV -> MP4 Converter
Run:  python converter.py
Needs: ffmpeg.exe in the same folder as this script (or in PATH)
"""
import http.server, subprocess, threading, webbrowser
import os, sys, json, uuid, tempfile, shutil, re
from urllib.parse import urlparse, parse_qs

PORT = 8765
JOBS  = {}
FILES = {}
TEMP  = tempfile.mkdtemp(prefix="mkv2mp4_")
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
FFMPEG = None

# ---------- find ffmpeg ----------
def find_ffmpeg():
    for candidate in [
        os.path.join(SCRIPT_DIR, "ffmpeg.exe"),
        os.path.join(SCRIPT_DIR, "ffmpeg"),
        shutil.which("ffmpeg") or "",
        r"C:\ffmpeg\bin\ffmpeg.exe",
        r"C:\Program Files\ffmpeg\bin\ffmpeg.exe",
        "/usr/local/bin/ffmpeg",
        "/opt/homebrew/bin/ffmpeg",
        "/usr/bin/ffmpeg",
    ]:
        if candidate and os.path.isfile(candidate):
            return candidate
    return None

def find_ffprobe():
    for candidate in [
        os.path.join(SCRIPT_DIR, "ffprobe.exe"),
        os.path.join(SCRIPT_DIR, "ffprobe"),
        shutil.which("ffprobe") or "",
        r"C:\ffmpeg\bin\ffprobe.exe",
        "/usr/local/bin/ffprobe",
        "/opt/homebrew/bin/ffprobe",
        "/usr/bin/ffprobe",
    ]:
        if candidate and os.path.isfile(candidate):
            return candidate
    return None

# ---------- helpers ----------
def get_duration(path):
    ffprobe = find_ffprobe()
    if not ffprobe:
        return None
    try:
        r = subprocess.run(
            [ffprobe, "-v", "quiet", "-print_format", "json", "-show_format", path],
            capture_output=True, text=True, timeout=30
        )
        return float(json.loads(r.stdout)["format"]["duration"])
    except Exception:
        return None

def parse_time(s):
    try:
        h, m, sec = s.split(":")
        return float(h)*3600 + float(m)*60 + float(sec)
    except Exception:
        return None

def fmt_bytes(n):
    for unit in ["B","KB","MB","GB"]:
        if n < 1024 or unit == "GB":
            return f"{n:.1f} {unit}"
        n /= 1024

# ---------- conversion worker ----------
def run_job(job_id, mkv_path, srt_path, out_path, cmd):
    job = JOBS[job_id]
    job.update(status="running", progress=0, message="Analysing input...")
    duration = get_duration(mkv_path)
    job["message"] = "Converting..."
    stderr_buf = []
    try:
        proc = subprocess.Popen(cmd, stderr=subprocess.PIPE, stdout=subprocess.DEVNULL)
        while True:
            raw = proc.stderr.readline()
            if not raw:
                break
            line = raw.decode("utf-8", errors="replace").strip()
            if not line:
                continue
            stderr_buf.append(line)
            m = re.search(r"time=(\d+:\d+:\d+\.\d+)", line)
            if m and duration:
                t = parse_time(m.group(1))
                if t is not None:
                    job["progress"] = min(int(t / duration * 100), 99)
            if any(k in line for k in ("frame=","size=","bitrate=","speed=")):
                job["detail"] = line
        proc.wait()
        if proc.returncode == 0 and os.path.exists(out_path) and os.path.getsize(out_path) > 0:
            job.update(status="done", progress=100, message="Done!",
                       out_path=out_path,
                       out_size_str=fmt_bytes(os.path.getsize(out_path)))
        else:
            tail = " | ".join(stderr_buf[-6:]) if stderr_buf else "(no output)"
            job.update(status="error", message=f"ffmpeg error (code {proc.returncode}): {tail[:400]}")
    except Exception as e:
        job.update(status="error", message=str(e))
    finally:
        for p in [mkv_path, srt_path]:
            if p and os.path.exists(p):
                try: os.unlink(p)
                except: pass

# ---------- HTTP handler ----------
class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *_): pass

    def send_json(self, data, code=200):
        body = json.dumps(data).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()

    def do_GET(self):
        path = urlparse(self.path).path

        if path in ("/", "/index.html"):
            body = HTML.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        if path.startswith("/status/"):
            jid = path[8:]
            job = JOBS.get(jid)
            if not job:
                self.send_json({"error": "not found"}, 404); return
            self.send_json({k:v for k,v in job.items() if k != "out_path"})
            return

        if path.startswith("/download/"):
            jid = path[10:]
            job = JOBS.get(jid)
            if not job or job.get("status") != "done":
                self.send_json({"error": "not ready"}, 404); return
            out  = job["out_path"]
            name = os.path.basename(out)
            size = os.path.getsize(out)
            self.send_response(200)
            self.send_header("Content-Type", "video/mp4")
            self.send_header("Content-Length", str(size))
            self.send_header("Content-Disposition", f'attachment; filename="{name}"')
            self.end_headers()
            with open(out, "rb") as f:
                shutil.copyfileobj(f, self.wfile)
            threading.Timer(30, lambda: os.unlink(out) if os.path.exists(out) else None).start()
            return

        self.send_json({"error": "not found"}, 404)

    def do_POST(self):
        parsed = urlparse(self.path)
        path   = parsed.path
        qs     = parse_qs(parsed.query)
        length = int(self.headers.get("Content-Length", 0))

        # ---- upload chunk ----
        if path == "/upload":
            uid    = qs.get("id",    [""])[0]
            ftype  = qs.get("ftype", ["mkv"])[0]   # "mkv" or "srt"
            index  = int(qs.get("chunk", ["0"])[0])
            total  = int(qs.get("total", ["1"])[0])
            name   = qs.get("name",  ["file.mkv"])[0]

            data = self.rfile.read(length)
            key  = f"{uid}_{ftype}"

            if key not in FILES:
                ext = name.rsplit(".", 1)[-1].lower() if "." in name else ftype
                tmp = os.path.join(TEMP, f"{key}.{ext}")
                FILES[key] = {"path": tmp, "name": name, "ext": ext,
                              "chunks_received": 0, "total_chunks": total}

            rec  = FILES[key]
            mode = "wb" if index == 0 else "ab"
            with open(rec["path"], mode) as f:
                f.write(data)
            rec["chunks_received"] += 1
            done = rec["chunks_received"] >= rec["total_chunks"]
            self.send_json({"ok": True, "done": done})
            return

        # ---- start conversion ----
        if path == "/convert":
            body = json.loads(self.rfile.read(length)) if length else {}

            if not FFMPEG:
                self.send_json({"error":
                    f"ffmpeg not found. Place ffmpeg.exe in: {SCRIPT_DIR}"}, 503)
                return

            uid     = body.get("uid", "")
            mkv_key = f"{uid}_mkv"
            srt_key = f"{uid}_srt"

            mkv_rec = FILES.get(mkv_key)
            srt_rec = FILES.get(srt_key)

            if not mkv_rec:
                self.send_json({"error": f"MKV not found (key={mkv_key})"}, 400); return
            if mkv_rec["chunks_received"] < mkv_rec["total_chunks"]:
                self.send_json({"error": "MKV upload incomplete"}, 400); return

            vcodec   = body.get("vcodec",   "libx264")
            acodec   = body.get("acodec",   "aac")
            crf      = str(body.get("crf",  23))
            abitrate = body.get("abitrate", "128k")
            sub_mode = body.get("sub_mode", "embed")

            mkv_path = mkv_rec["path"]
            srt_path = srt_rec["path"] if (srt_rec and srt_rec["chunks_received"] >= srt_rec["total_chunks"]) else None
            srt_ext  = srt_rec["ext"]  if srt_rec else "srt"
            out_name = mkv_rec["name"].rsplit(".", 1)[0] + ".mp4"
            out_path = os.path.join(TEMP, f"{uid}_output.mp4")

            # build command
            cmd = [FFMPEG, "-i", mkv_path]
            if srt_path:
                cmd += ["-i", srt_path]

            cmd += ["-c:v", vcodec]
            if vcodec != "copy":
                cmd += ["-crf", crf]
                if vcodec == "libx264":
                    cmd += ["-preset", "fast", "-pix_fmt", "yuv420p"]

            cmd += ["-c:a", acodec]
            if acodec != "copy":
                cmd += ["-b:a", abitrate]

            if sub_mode == "embed":
                if srt_path:
                    cmd += ["-map", "0:v:0", "-map", "0:a:0", "-map", "1:0", "-c:s", "mov_text"]
                else:
                    cmd += ["-map", "0", "-c:s", "mov_text", "-ignore_unknown"]
            elif sub_mode == "burn":
                sp = srt_path if srt_path else mkv_path
                if sys.platform == "win32":
                    sp = sp.replace("\\", "/").replace(":", "\\:")
                cmd += ["-vf", f"subtitles={sp}", "-sn"]
            else:
                cmd += ["-map", "0:v:0", "-map", "0:a?", "-sn"]

            cmd += ["-stats", "-movflags", "+faststart", "-y", out_path]

            jid = uuid.uuid4().hex[:8]
            JOBS[jid] = {"status": "queued", "progress": 0,
                         "message": "Queued", "detail": "", "out_name": out_name}

            print(f"  [job {jid}] {' '.join(cmd)}")

            threading.Thread(target=run_job,
                             args=(jid, mkv_path, srt_path, out_path, cmd),
                             daemon=True).start()
            self.send_json({"job_id": jid})
            return

        self.send_json({"error": "unknown endpoint"}, 404)

# ---------- HTML ----------
HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>MKV to MP4</title>
<style>
:root{--bg:#0d0d0d;--sf:#161616;--sf2:#1e1e1e;--br:#2a2a2a;--br2:#333;
  --tx:#f0f0f0;--mu:#888;--ac:#e8ff47;--ac2:#b8cc30;
  --ok:#4ade80;--er:#f87171;--bl:#60a5fa;
  --r:10px;--fn:'Courier New',Consolas,monospace}
*{box-sizing:border-box;margin:0;padding:0}
body{background:var(--bg);color:var(--tx);font-family:var(--fn);
  min-height:100vh;display:flex;justify-content:center;padding:2rem 1rem 4rem}
.w{width:100%;max-width:740px}
h1{font-size:2.2rem;font-weight:normal;letter-spacing:-1px;margin-bottom:2.5rem}
h1 span{color:var(--ac)}
h1 small{display:block;font-size:13px;color:var(--mu);margin-top:8px;font-weight:normal;letter-spacing:0}
.notice{font-size:11px;color:var(--mu);background:var(--sf);border-left:3px solid var(--br2);
  padding:10px 14px;border-radius:0 6px 6px 0;margin-bottom:16px;line-height:1.6}
.notice strong{color:var(--tx)} .notice a{color:var(--bl)}
.notice.warn{border-left-color:var(--er)} .notice.warn strong{color:var(--er)}
.zones{display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-bottom:20px}
.zone{border:1.5px dashed var(--br2);border-radius:var(--r);padding:1.6rem 1rem;
  text-align:center;cursor:pointer;background:var(--sf);position:relative;
  transition:border-color .2s,background .2s}
.zone:hover,.zone.ov{border-color:var(--ac);background:#141400}
.zone.has{border-style:solid;border-color:var(--ac);background:#141400}
.zone input{display:none}
.zi{font-size:26px;margin-bottom:8px;display:block}
.zl{font-size:11px;color:var(--mu);text-transform:uppercase;letter-spacing:1.5px}
.zn{font-size:12px;color:var(--ac);margin-top:6px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;padding:0 8px}
.zx{position:absolute;top:7px;right:9px;background:none;border:none;color:var(--mu);cursor:pointer;font-size:15px;display:none}
.zone.has .zx{display:block}
.zbar{height:3px;background:var(--br2);border-radius:0 0 var(--r) var(--r);
  position:absolute;bottom:0;left:0;right:0;overflow:hidden;display:none}
.zone.uploading .zbar{display:block}
.zbar-f{height:100%;background:var(--ac);transition:width .2s;width:0}
.panel{background:var(--sf);border:1px solid var(--br);border-radius:var(--r);
  padding:1.2rem 1.4rem;margin-bottom:20px}
.pt{font-size:11px;text-transform:uppercase;letter-spacing:2px;color:var(--mu);margin-bottom:14px}
.g2{display:grid;grid-template-columns:1fr 1fr;gap:14px}
.fl label{font-size:11px;color:var(--mu);display:block;margin-bottom:6px;
  text-transform:uppercase;letter-spacing:1px}
.fl select{width:100%;background:var(--sf2);border:1px solid var(--br2);color:var(--tx);
  font-family:var(--fn);font-size:13px;padding:7px 10px;border-radius:6px;cursor:pointer;outline:none}
.fl select:focus{border-color:var(--ac)}
.rr{display:flex;align-items:center;gap:10px}
.rr input[type=range]{flex:1;height:4px;accent-color:var(--ac)}
.rv{font-size:13px;min-width:28px;text-align:right;color:var(--ac)}
.rg{display:flex;flex-direction:column;gap:10px}
.ro{display:flex;align-items:flex-start;gap:10px;cursor:pointer}
.ro input{accent-color:var(--ac);margin-top:2px}
.rot{font-size:13px;color:var(--tx)} .rod{font-size:11px;color:var(--mu);margin-top:2px}
.btn{width:100%;padding:14px;background:var(--ac);color:#0d0d0d;border:none;
  border-radius:var(--r);font-family:var(--fn);font-size:14px;font-weight:bold;
  letter-spacing:2px;text-transform:uppercase;cursor:pointer;margin-bottom:20px;
  transition:background .2s,transform .1s}
.btn:hover:not(:disabled){background:var(--ac2)}
.btn:active:not(:disabled){transform:scale(.99)}
.btn:disabled{background:#2a2a2a;color:#555;cursor:not-allowed}
.scard{background:var(--sf);border:1px solid var(--br);border-radius:var(--r);
  padding:1.2rem 1.4rem;margin-bottom:20px;display:none}
.scard.on{display:block}
.srow{display:flex;align-items:center;gap:12px;margin-bottom:14px}
.spin{width:18px;height:18px;border:2px solid var(--br2);border-top-color:var(--ac);
  border-radius:50%;animation:sp .7s linear infinite;flex-shrink:0}
.spin.ok{border-color:var(--ok);border-top-color:var(--ok);animation:none}
.spin.err{border-color:var(--er);border-top-color:var(--er);animation:none}
@keyframes sp{to{transform:rotate(360deg)}}
.smain{font-size:13px;color:var(--tx);flex:1}
.stimer{font-size:12px;color:var(--mu);min-width:40px;text-align:right}
.pb{height:4px;background:var(--br2);border-radius:2px;overflow:hidden;margin-bottom:8px}
.pf{height:100%;background:var(--ac);transition:width .4s;border-radius:2px}
.prow{display:flex;justify-content:space-between;font-size:11px;color:var(--mu);margin-bottom:12px}
.slog{font-size:11px;color:var(--mu);max-height:70px;overflow-y:auto;
  line-height:1.6;border-top:1px solid var(--br);padding-top:8px;word-break:break-all}
.dla{background:#091500;border:1.5px solid var(--ok);border-radius:var(--r);
  padding:1.2rem 1.4rem;display:none;align-items:center;gap:16px}
.dla.on{display:flex}
.bdl{padding:10px 22px;background:var(--ok);color:#091500;border:none;border-radius:8px;
  font-family:var(--fn);font-size:13px;font-weight:bold;text-decoration:none;
  display:inline-block;text-transform:uppercase;letter-spacing:1px;white-space:nowrap;cursor:pointer}
.bdl:hover{opacity:.85}
@media(max-width:520px){.zones,.g2{grid-template-columns:1fr}}
</style>
</head>
<body>
<div class="w">
  <h1>MKV <span>&rarr;</span> MP4
    <small>Native ffmpeg &middot; Subtitles preserved &middot; Runs locally</small>
  </h1>

  <div id="no-ffmpeg" class="notice warn" style="display:none">
    <strong>ffmpeg not found.</strong>
    Place <strong>ffmpeg.exe</strong> in the same folder as converter.py, then restart.
    Download: <a href="https://www.gyan.dev/ffmpeg/builds/" target="_blank">gyan.dev/ffmpeg/builds</a>
    &rarr; ffmpeg-release-essentials.zip &rarr; extract bin/ffmpeg.exe
  </div>

  <div class="notice">
    <strong>Runs locally.</strong>
    Files stay on your PC. Keep the terminal open while converting.
  </div>

  <div class="zones">
    <div class="zone" id="zone-mkv" onclick="document.getElementById('inp-mkv').click()">
      <input type="file" id="inp-mkv" accept=".mkv,video/x-matroska">
      <span class="zi">&#127902;</span>
      <div class="zl">MKV File</div>
      <div class="zn" id="lbl-mkv">Click or drop here</div>
      <button class="zx" onclick="clearZone('mkv',event)">&#10005;</button>
      <div class="zbar"><div class="zbar-f" id="bar-mkv"></div></div>
    </div>
    <div class="zone" id="zone-srt" onclick="document.getElementById('inp-srt').click()">
      <input type="file" id="inp-srt" accept=".srt,.ass,.ssa">
      <span class="zi">&#128172;</span>
      <div class="zl">Subtitle (optional)</div>
      <div class="zn" id="lbl-srt">Click or drop here</div>
      <button class="zx" onclick="clearZone('srt',event)">&#10005;</button>
      <div class="zbar"><div class="zbar-f" id="bar-srt"></div></div>
    </div>
  </div>

  <div class="panel">
    <div class="pt">Subtitle handling</div>
    <div class="rg">
      <label class="ro"><input type="radio" name="sub" value="embed" checked>
        <div><div class="rot">Embed in MP4</div><div class="rod">Soft — togglable in any player</div></div></label>
      <label class="ro"><input type="radio" name="sub" value="burn">
        <div><div class="rot">Burn into video</div><div class="rod">Hardcoded — always visible</div></div></label>
      <label class="ro"><input type="radio" name="sub" value="strip">
        <div><div class="rot">Strip subtitles</div><div class="rod">Remove all subtitle tracks</div></div></label>
    </div>
  </div>

  <div class="panel">
    <div class="pt">Encoding</div>
    <div class="g2">
      <div class="fl"><label>Video codec</label>
        <select id="vc" onchange="document.getElementById('crf-row').style.opacity=this.value=='copy'?'0.4':'1'">
          <option value="libx264" selected>H.264 — best compat</option>
          <option value="libx265">H.265 — smaller file</option>
          <option value="copy">Stream copy (fastest)</option>
        </select></div>
      <div class="fl"><label>Audio codec</label>
        <select id="ac">
          <option value="aac" selected>AAC — best compat</option>
          <option value="libmp3lame">MP3</option>
          <option value="copy">Stream copy</option>
        </select></div>
      <div class="fl" id="crf-row"><label>Quality CRF (lower = better)</label>
        <div class="rr">
          <input type="range" id="crf" min="16" max="32" value="23" step="1"
            oninput="document.getElementById('crfv').textContent=this.value">
          <span class="rv" id="crfv">23</span>
        </div></div>
      <div class="fl"><label>Audio bitrate</label>
        <select id="ab">
          <option value="96k">96 kbps</option>
          <option value="128k" selected>128 kbps</option>
          <option value="192k">192 kbps</option>
          <option value="256k">256 kbps</option>
        </select></div>
    </div>
  </div>

  <button class="btn" id="btn" onclick="go()">Convert &rarr;</button>

  <div class="scard" id="scard">
    <div class="srow">
      <div class="spin" id="spin"></div>
      <div class="smain" id="smain">Starting...</div>
      <div class="stimer" id="stimer">0s</div>
    </div>
    <div class="pb"><div class="pf" id="pf" style="width:0%"></div></div>
    <div class="prow"><span id="pstep">-</span><span id="ppct">0%</span></div>
    <div class="slog" id="slog"></div>
  </div>

  <div class="dla" id="dla">
    <div style="font-size:32px">&#9989;</div>
    <div style="flex:1">
      <div style="font-size:14px;color:var(--ok)" id="dname">output.mp4</div>
      <div style="font-size:11px;color:var(--mu);margin-top:4px" id="dsize"></div>
    </div>
    <a class="bdl" id="dlink" href="#">Download</a>
  </div>
</div>

<script>
const CHUNK = 2 * 1024 * 1024;
let mkvFile = null, srtFile = null;
let uid = null, jobId = null;
let timerID = null, pollID = null, startT = 0;

// check ffmpeg on load
fetch('/convert', {method:'POST', headers:{'Content-Type':'application/json'}, body:'{}'})
  .then(r=>r.json()).then(d=>{
    if (d.error && d.error.includes('not found'))
      document.getElementById('no-ffmpeg').style.display='block';
  }).catch(()=>{});

// file inputs
document.getElementById('inp-mkv').onchange = e => {
  if (e.target.files[0]) pickFile('mkv', e.target.files[0]);
};
document.getElementById('inp-srt').onchange = e => {
  if (e.target.files[0]) pickFile('srt', e.target.files[0]);
};

// drag and drop
['mkv','srt'].forEach(t => {
  const z = document.getElementById('zone-'+t);
  z.addEventListener('dragover', e => { e.preventDefault(); z.classList.add('ov'); });
  z.addEventListener('dragleave', () => z.classList.remove('ov'));
  z.addEventListener('drop', e => {
    e.preventDefault(); z.classList.remove('ov');
    const f = e.dataTransfer.files[0]; if (!f) return;
    if (t === 'mkv' && (f.name.toLowerCase().endsWith('.mkv') || f.type.includes('matroska')))
      pickFile('mkv', f);
    else if (t === 'srt' && /[.](srt|ass|ssa)$/i.test(f.name))
      pickFile('srt', f);
    else
      alert('Drop a .' + t + ' file here');
  });
});

function pickFile(t, f) {
  if (t === 'mkv') mkvFile = f; else srtFile = f;
  document.getElementById('zone-'+t).classList.add('has');
  document.getElementById('lbl-'+t).textContent = f.name + ' (' + fmtB(f.size) + ')';
}
function clearZone(t, e) {
  e.stopPropagation();
  if (t === 'mkv') { mkvFile = null; document.getElementById('inp-mkv').value = ''; }
  else             { srtFile = null; document.getElementById('inp-srt').value = ''; }
  document.getElementById('zone-'+t).classList.remove('has','uploading');
  document.getElementById('lbl-'+t).textContent = 'Click or drop here';
  document.getElementById('bar-'+t).style.width = '0%';
}

// ui helpers
function setMain(s)  { document.getElementById('smain').textContent  = s; }
function setStep(s)  { document.getElementById('pstep').textContent  = s; }
function setLog(s)   { document.getElementById('slog').textContent   = s; }
function setPct(p) {
  p = Math.min(Math.max(p,0),100);
  document.getElementById('pf').style.width = p+'%';
  document.getElementById('ppct').textContent = Math.round(p)+'%';
}
function setSpin(s) {
  document.getElementById('spin').className = 'spin'+(s?' '+s:'');
}
function startClock() {
  startT = Date.now();
  timerID = setInterval(() => {
    document.getElementById('stimer').textContent =
      Math.round((Date.now()-startT)/1000)+'s';
  }, 1000);
}
function stopClock() { clearInterval(timerID); }

// chunked upload
async function uploadFile(file, ftype) {
  const total = Math.ceil(file.size / CHUNK);
  document.getElementById('zone-'+ftype).classList.add('uploading');
  for (let i = 0; i < total; i++) {
    const chunk = file.slice(i*CHUNK, (i+1)*CHUNK);
    const buf   = await chunk.arrayBuffer();
    const r = await fetch(
      `/upload?id=${uid}&ftype=${ftype}&chunk=${i}&total=${total}&name=${encodeURIComponent(file.name)}`,
      { method:'POST', body: buf }
    );
    if (!r.ok) throw new Error('Upload chunk failed');
    document.getElementById('bar-'+ftype).style.width = Math.round((i+1)/total*100)+'%';
  }
  document.getElementById('zone-'+ftype).classList.remove('uploading');
}

// poll job
function poll(jid, outName) {
  pollID = setInterval(async () => {
    try {
      const d = await fetch('/status/'+jid).then(r=>r.json());
      setPct(d.progress || 0);
      if (d.detail) setLog(d.detail);
      if (d.status === 'done') {
        clearInterval(pollID); stopClock();
        setSpin('ok');
        const t = ((Date.now()-startT)/1000).toFixed(1);
        setMain('Done in '+t+'s'); setStep('Complete'); setPct(100);
        document.getElementById('dname').textContent = outName;
        document.getElementById('dsize').textContent = d.out_size_str || '';
        const lnk = document.getElementById('dlink');
        lnk.href = '/download/'+jid; lnk.download = outName;
        document.getElementById('dla').classList.add('on');
        document.getElementById('btn').disabled = false;
      } else if (d.status === 'error') {
        clearInterval(pollID); stopClock();
        setSpin('err'); setMain(d.message || 'Error'); setStep('Failed');
        document.getElementById('btn').disabled = false;
      } else {
        setMain(d.message || 'Converting...'); setStep('Converting...');
      }
    } catch(_) {}
  }, 800);
}

// main
async function go() {
  if (!mkvFile) { alert('Select an MKV file first.'); return; }
  clearInterval(pollID); stopClock();
  document.getElementById('dla').classList.remove('on');
  document.getElementById('slog').textContent = '';
  document.getElementById('scard').classList.add('on');
  document.getElementById('btn').disabled = true;
  setSpin(''); setPct(0); startClock();

  uid = Date.now().toString(36) + Math.random().toString(36).slice(2,8);
  const outName = mkvFile.name.replace(/[.]mkv$/i,'') + '.mp4';

  try {
    setMain('Uploading MKV...'); setStep('Upload'); setPct(2);
    await uploadFile(mkvFile, 'mkv');

    if (srtFile) {
      setMain('Uploading subtitle...'); setStep('Upload subtitle');
      await uploadFile(srtFile, 'srt');
    }

    setMain('Starting ffmpeg...'); setStep('Starting...'); setPct(8);

    const resp = await fetch('/convert', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        uid,
        vcodec:   document.getElementById('vc').value,
        acodec:   document.getElementById('ac').value,
        crf:      parseInt(document.getElementById('crf').value),
        abitrate: document.getElementById('ab').value,
        sub_mode: document.querySelector('input[name=sub]:checked').value
      })
    });
    const data = await resp.json();
    if (data.error) {
      setSpin('err'); setMain(data.error); setStep('Failed'); stopClock();
      document.getElementById('btn').disabled = false;
      return;
    }
    setMain('Converting...'); setStep('Converting...'); setPct(10);
    poll(data.job_id, outName);

  } catch(err) {
    stopClock(); setSpin('err');
    setMain('Error: '+(err.message||String(err))); setStep('Failed');
    document.getElementById('btn').disabled = false;
  }
}

function fmtB(n) {
  if (n<1024) return n+' B';
  if (n<1048576) return (n/1024).toFixed(1)+' KB';
  if (n<1073741824) return (n/1048576).toFixed(1)+' MB';
  return (n/1073741824).toFixed(2)+' GB';
}
</script>
</body>
</html>"""

# ---------- main ----------
def main():
    global FFMPEG
    FFMPEG = find_ffmpeg()

    print("="*58)
    print("  MKV to MP4 Converter")
    print("="*58)
    if FFMPEG:
        print(f"  ffmpeg : {FFMPEG}")
    else:
        print(f"  ffmpeg : NOT FOUND")
        print(f"  Place ffmpeg.exe here: {SCRIPT_DIR}")
    print(f"  Temp   : {TEMP}")
    print(f"  URL    : http://localhost:{PORT}")
    print("  Ctrl+C to stop")
    print("="*58)

    threading.Timer(1.2, lambda: webbrowser.open(f"http://localhost:{PORT}")).start()
    try:
        with http.server.HTTPServer(("", PORT), Handler) as srv:
            srv.serve_forever()
    except KeyboardInterrupt:
        print("\n  Stopped. Cleaning up...")
        shutil.rmtree(TEMP, ignore_errors=True)
    except OSError:
        print(f"\n  Port {PORT} busy. Open: http://localhost:{PORT}")
        sys.exit(1)

if __name__ == "__main__":
    main()
