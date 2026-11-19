import json
import os
import subprocess
from pathlib import Path

connection = json.loads(Path('runtime.example.json').read_text(encoding='utf-8'))['providerConnection']
key = os.environ['AISPOT_DS_API_KEY']
payload = json.dumps({'model': connection['modelId'], 'stream': False, 'max_tokens': 128,
                      'thinking': {'type': 'disabled'},
                      'messages': [{'role': 'user', 'content': 'Reply with PROVIDER_READY only.'}]})
request = '\n'.join([
    'url = ' + json.dumps(connection['baseUrl'] + '/chat/completions'),
    'header = ' + json.dumps('Authorization: Bearer ' + key),
    'header = "Content-Type: application/json"',
    'data = ' + json.dumps(payload),
    'silent', 'show-error', 'connect-timeout = 15', 'max-time = 60',
    'proxy = "http://127.0.0.1:7897"'
])
result = subprocess.run(['curl.exe', '--config', '-'], input=request, text=True,
                        capture_output=True, timeout=70, creationflags=0x08000000)
try:
    data = json.loads(result.stdout)
    print(json.dumps({'model': data.get('model'), 'usage': data.get('usage'),
                      'answer': data.get('choices', [{}])[0].get('message', {}).get('content'),
                      'error': data.get('error')}))
except ValueError:
    print(json.dumps({'exitCode': result.returncode, 'error': result.stderr.replace(key, '[REDACTED]')}))
raise SystemExit(result.returncode)
