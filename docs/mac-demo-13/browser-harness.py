from pathlib import Path
import json, subprocess, tempfile, time, urllib.request, threading, sys
root=Path(__file__).resolve().parents[2]
sys.path[:0]=[str(root),str(root/'tests')]
import pytest, uvicorn
from server.app import create_app
from server.model import Settings
from tools import run
import test_mac_demo as fixtures
workspace=Path(tempfile.mkdtemp(prefix='d4-mac-browser-',dir='/private/tmp'))
workspace.chmod(0o700)
monkey=pytest.MonkeyPatch()
pem=fixtures.pem.__wrapped__()
config=fixtures.mac_files.__wrapped__(workspace,pem,monkey)
table,messages,assertions,faults=fixtures.providers.__wrapped__(config,pem,monkey)
apps=[]
monkey.setattr(run.uvicorn,'run',lambda app,**kwargs:apps.append(app))
run.main(['--mac-demo-live','--registry-dir',str(workspace/'live-registry')])
servers=[]
processes=[]
try:
 for port,app in [(4352,create_app(Settings(data_dir=workspace/'ordinary-registry'))),(4353,apps[0])]:
  server=uvicorn.Server(uvicorn.Config(app,host='127.0.0.1',port=port,access_log=False,log_level='warning',proxy_headers=False))
  thread=threading.Thread(target=server.run,daemon=True)
  thread.start()
  servers.append((server,thread))
 static_log=(workspace/'static.log').open('w')
 static=subprocess.Popen([str(root/'.venv/bin/python'),'-B',str(root/'tools/serve.py'),'--port','4351'],stdout=static_log,stderr=static_log)
 processes.append(static)
 profile=workspace/'chrome-profile'
 browser_log=(workspace/'browser.log').open('w')
 browser=subprocess.Popen(['/Applications/Google Chrome.app/Contents/MacOS/Google Chrome','--headless=new','--disable-gpu','--use-mock-keychain','--no-first-run','--no-default-browser-check','--disable-extensions','--disable-background-networking','--disable-component-update','--disable-sync','--remote-debugging-address=127.0.0.1','--remote-debugging-port=0','--user-data-dir='+str(profile),'about:blank'],stdout=browser_log,stderr=browser_log)
 processes.append(browser)
 deadline=time.monotonic()+30
 endpoint=None
 while time.monotonic()<deadline:
  port_file=profile/'DevToolsActivePort'
  if port_file.exists() and all(s.started for s,t in servers):
   candidate='http://127.0.0.1:'+port_file.read_text().splitlines()[0]
   try:
    with urllib.request.urlopen(candidate+'/json/version',timeout=1) as response:json.load(response)
    with urllib.request.urlopen('http://127.0.0.1:4351/',timeout=1) as response:assert response.status==200
    endpoint=candidate
    break
   except OSError:pass
  time.sleep(.3)
 if endpoint is None:raise RuntimeError('Browser or local servers failed to start')
 subprocess.run(['node',str(root/'docs/mac-demo-13/browser-check.mjs'),endpoint],cwd=root,check=True,timeout=160)
 assert len(messages)==len(assertions)==table.append_count==1
 assert table.calls==['header','scan','append','readback']
 report={'mode':'Offline artificial RSA key and httpx.MockTransport only','telegram_attempts':len(messages),'oauth_requests':len(assertions),'sheets_appends':table.append_count,'sheets_calls':table.calls,'same_submission_id':table.rows[0][0] in messages[0][0]}
 assert report['same_submission_id']
 (root/'docs/mac-demo-13/browser-adapters.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
 print(json.dumps(report,ensure_ascii=False))
finally:
 for server,thread in servers:server.should_exit=True
 for server,thread in servers:thread.join(timeout=5)
 for process in reversed(processes):
  process.terminate()
  try:process.wait(timeout=8)
  except subprocess.TimeoutExpired:process.kill();process.wait(timeout=5)
 monkey.undo()
 print('Own browser and offline servers stopped',flush=True)
