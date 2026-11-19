import json
import os
import urllib.request
from pathlib import Path

connection = json.loads(Path('runtime.example.json').read_text(encoding='utf-8'))['providerConnection']
request = urllib.request.Request(connection['baseUrl'] + '/models', headers={
    'Authorization': 'Bearer ' + os.environ['AISPOT_DS_API_KEY']
})
response = json.load(urllib.request.urlopen(request, timeout=45))
model = next(item for item in response['data'] if item['id'] == connection['modelId'])
print(json.dumps({'id': model['id'], 'metadataKeys': list(model), 'capacity': {
    key: value for key, value in model.items()
    if any(part in key.lower() for part in ('context', 'max_tokens', 'token_limit'))
}}))
