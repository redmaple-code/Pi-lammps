import { spawn } from 'node:child_process';

// cURL receives request headers and JSON through stdin, never process arguments or files.
export function createCurlFetch({ executable = process.platform === 'win32' ? 'curl.exe' : 'curl', proxy } = {}) {
  return async (input, init) => {
    const request = new Request(input, init);
    const body = await request.text();
    request.signal.throwIfAborted();
    const configuration = [
      `url = ${JSON.stringify(request.url)}`, `request = ${JSON.stringify(request.method)}`,
      'silent', 'show-error', 'include', 'no-buffer', 'suppress-connect-headers',
      ...[...request.headers].map(([name, value]) => `header = ${JSON.stringify(`${name}: ${value}`)}`)
    ];
    if (body) configuration.push(`data-binary = ${JSON.stringify(body)}`);
    if (proxy) configuration.push(`proxy = ${JSON.stringify(proxy)}`);
    return new Promise((resolve, reject) => {
      let pending = Buffer.alloc(0), headersReceived = false, bodyClosed = false, controller;
      const child = spawn(executable, ['--config', '-'], { windowsHide: true, stdio: ['pipe', 'pipe', 'pipe'] });
      const stream = new ReadableStream({ start(value) { controller = value; }, cancel() { bodyClosed = true; child.kill(); } });
      const fail = error => {
        if (!headersReceived) reject(error);
        else if (!bodyClosed) { bodyClosed = true; controller.error(error); }
      };
      const abort = () => { child.kill(); fail(request.signal.reason || new DOMException('Request aborted', 'AbortError')); };
      request.signal.addEventListener('abort', abort, { once: true });
      child.once('error', fail);
      child.stdin.on('error', error => { if (error.code !== 'EPIPE') fail(error); });
      // Error bodies are read from stdout. Transport diagnostics deliberately omit request data.
      child.stderr.resume();
      child.stdout.on('data', chunk => {
        if (bodyClosed) return;
        if (headersReceived) { controller.enqueue(new Uint8Array(chunk)); return; }
        pending = Buffer.concat([pending, chunk]);
        let boundary;
        while ((boundary = pending.indexOf('\r\n\r\n')) >= 0) {
          const block = pending.subarray(0, boundary).toString('utf8');
          pending = pending.subarray(boundary + 4);
          const lines = block.split('\r\n');
          const status = Number(lines.shift()?.match(/^HTTP\/\S+\s+(\d{3})/)?.[1]);
          if (status >= 100 && status < 200) continue;
          if (!Number.isInteger(status) || status < 200 || status > 599) { fail(new Error('Invalid cURL HTTP status')); child.kill(); return; }
          const headers = new Headers();
          for (const line of lines) {
            const colon = line.indexOf(':');
            if (colon > 0) headers.append(line.slice(0, colon), line.slice(colon + 1).trim());
          }
          headersReceived = true;
          const empty = [204, 205, 304].includes(status);
          if (empty) { bodyClosed = true; controller.close(); }
          resolve(new Response(empty ? null : stream, { status, headers }));
          if (pending.length && !empty) controller.enqueue(new Uint8Array(pending));
          pending = Buffer.alloc(0);
          break;
        }
      });
      child.once('close', code => {
        request.signal.removeEventListener('abort', abort);
        if (code !== 0 && !bodyClosed) fail(Object.assign(new Error(`cURL transport exited with code ${code}`), { code: 'CURL_TRANSPORT_FAILED', exitCode: code }));
        else if (!headersReceived) fail(new Error('cURL returned no HTTP response'));
        else if (!bodyClosed) { bodyClosed = true; controller.close(); }
      });
      child.stdin.end(configuration.join('\n') + '\n', 'utf8');
      if (request.signal.aborted) abort();
    });
  };
}
