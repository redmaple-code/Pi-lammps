import { execFile } from 'node:child_process';
import { promisify } from 'node:util';
import { requireCondition, utc } from '../contracts/index.js';

const execute = promisify(execFile);
const quote = value => `'${String(value).replaceAll("'", "'\\''")}'`;
export class SlurmExecutor {
  constructor({ host, ssh = 'ssh', store, timeoutMs = 30000 }) {
    requireCondition(/^[a-zA-Z0-9_.@-]+$/.test(host) && !host.startsWith('-'), 'SCHEMA_INVALID', 'Invalid SSH host');
    Object.assign(this, { host, ssh, store, timeoutMs });
  }
  async remote(argv) {
    const { stdout } = await execute(this.ssh, ['-oBatchMode=yes', this.host, argv.map(quote).join(' ')],
      { timeout: this.timeoutMs, maxBuffer: 1024 * 1024, windowsHide: true });
    return stdout.trim();
  }
  async submit(spec, operationId) {
    requireCondition(/^[a-zA-Z0-9_-]+$/.test(operationId), 'SCHEMA_INVALID', 'Invalid Slurm operation ID');
    const operation = this.store.operation(operationId);
    requireCondition(operation, 'REFERENCE_MISSING', 'Persist submit intent first');
    if (operation.status !== 'intent') return this.reconcile(operationId);
    this.store.claim(operationId, { host: this.host, token: operationId });
    try {
      const output = await this.remote(['sbatch', '--parsable', `--job-name=${operationId}`, spec.scriptPath]);
      const jobId = output.split(';')[0];
      requireCondition(/^\d+$/.test(jobId), 'SUBMISSION_UNKNOWN', 'Slurm job identity missing');
      this.store.put('remote-jobs', operationId, { jobId, host: this.host, operationId }, 0);
      return { operationId, jobId, status: 'queued' };
    } catch (error) { return { operationId, status: 'unknown', error: error.message }; }
  }
  async reconcile(operationId) {
    const known = this.store.get('remote-jobs', operationId);
    const result = await this.remote(['sacct', '-n', '-X', '-P', '--name', operationId, '--format=JobIDRaw,JobName,State,ExitCode']);
    const matches = result.split('\n').filter(Boolean).map(line => line.split('|')).filter(row => row[1] === operationId && /^\d+$/.test(row[0]));
    if (!matches.length) return { status: 'unknown', operationId, error: 'No confirmed scheduler record' };
    requireCondition(matches.length === 1, 'SUBMISSION_UNKNOWN', 'Multiple scheduler jobs share operation identity');
    const [jobId, , state, exitCode] = matches[0];
    if (known) requireCondition(known.jobId === jobId, 'IDENTITY_REQUIRED', 'Scheduler identity differs');
    const status = { COMPLETED: 'succeeded', FAILED: 'failed', TIMEOUT: 'failed', CANCELLED: 'cancelled', PENDING: 'queued', RUNNING: 'running' }[state.split(/[+ ]/)[0]] || 'unknown';
    const receipt = { operationId, jobId, status, schedulerState: state, exitCode, observedAt: utc() };
    if (['succeeded', 'failed', 'cancelled'].includes(status)) this.store.settle(operationId, receipt);
    return receipt;
  }
  async cancel(operationId) {
    const job = this.store.get('remote-jobs', operationId);
    requireCondition(job, 'RESULT_UNKNOWN', 'Reconcile job identity before cancellation');
    await this.remote(['scancel', job.jobId]);
    return { status: 'cancelRequested', operationId, jobId: job.jobId };
  }
}
