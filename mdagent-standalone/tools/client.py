"""Minimal stdlib host for an isolated module process; no automatic retries."""
import json
from pathlib import Path
import subprocess
import sys
import threading
import uuid

class ModuleProcess:
    def __init__(self, package, data_root, actor, *, allow_write=False, project_root=None):
        path=Path(package).resolve()
        command=[sys.executable,'-I','-S',str(path/'run.py'),'--data-root',str(Path(data_root).resolve()),'--actor',actor]
        if allow_write: command.append('--allow-write')
        if project_root is not None: command.extend(['--project-root',str(Path(project_root).resolve())])
        self._process=subprocess.Popen(command,stdin=subprocess.PIPE,stdout=subprocess.PIPE,
                                       text=True,encoding='utf-8',errors='strict',bufsize=1)
        self._lock=threading.Lock()

    def call(self, operation, data=None, request_id=None):
        request={'id':request_id or uuid.uuid4().hex,'operation':operation,'input':{} if data is None else data}
        with self._lock:
            if self._process.poll() is not None: raise RuntimeError('module process not running; reconcile prior writes')
            self._process.stdin.write(json.dumps(request,ensure_ascii=False,allow_nan=False)+'\n')
            self._process.stdin.flush()
            line=self._process.stdout.readline()
            if not line: raise RuntimeError('module process closed; reconcile prior writes')
            response=json.loads(line)
            if response.get('id')!=request['id']: raise RuntimeError('module protocol response id mismatch')
            return response

    def close(self):
        if self._process.stdin: self._process.stdin.close()
        self._process.wait(timeout=10)
        if self._process.stdout: self._process.stdout.close()

    def __enter__(self): return self
    def __exit__(self,*_): self.close()
