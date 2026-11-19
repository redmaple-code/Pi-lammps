import json
import os
import urllib.request
import urllib.error
from pathlib import Path

connection = json.loads(Path('runtime.example.json').read_text(encoding='utf-8'))['providerConnection']
key = os.environ['AISPOT_DS_API_KEY']
payload = {'model': connection['modelId'], 'stream': False, 'max_tokens': 128,
           'thinking': {'type': 'disabled'},
           'messages': [{'role': 'user', 'content': 'Reply with PROVIDER_READY only.'}]}
request = urllib.request.Request(connection['baseUrl'] + '/chat/completions',
                                 data=json.dumps(payload).encode(), headers={
                                     'Authorization': 'Bearer ' + key, 'Content-Type': 'application/json'})
try:
    response = urllib.request.urlopen(request, timeout=90)
    result = json.load(response)
    print(json.dumps({'status': response.status, 'model': result.get('model'), 'usage': result.get('usage'),
                      'answer': result.get('choices', [{}])[0].get('message', {}).get('content'),
                      'error': result.get('error')}))
except Exception as error:
    print(json.dumps({'error': str(error).replace(key, '[REDACTED]')}))
    raise SystemExit(1)
