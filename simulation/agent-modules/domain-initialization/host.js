import { appendFile, mkdir } from 'node:fs/promises';
import { join } from 'node:path';
import { ModuleHost } from '../core/index.js';
import { createDomainInitializationModule } from './index.js';
import { DomainInitializationService } from './service.js';
import { FileLibraryRepository } from './repository.js';

export async function openDomainInitialization({ directory, repository, audit } = {}) {
  await mkdir(directory, { recursive: true });
  const service = new DomainInitializationService({ repository: repository || new FileLibraryRepository(directory) });
  const host = new ModuleHost({
    audit: audit || { append: event => appendFile(join(directory, 'audit.jsonl'),
      `${JSON.stringify({ ...event, timestamp: new Date().toISOString() })}\n`, { mode: 0o600 }) },
    authorize: (actor, capability) => capability.startsWith('domain.initialize.')
      && (actor.kind === 'human' || !['domain.initialize.answer', 'domain.initialize.decide', 'domain.initialize.decideMany', 'domain.initialize.pause'].includes(capability))
  });
  host.register(createDomainInitializationModule({ service }));
  return { service, host, directory };
}
