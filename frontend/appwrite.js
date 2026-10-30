import { Client } from 'appwrite';
import { DEFAULT_CONFIG } from './data.js';

export const projectName = 'Tele-copier';

export function createAppwriteClient({ endpoint, projectId } = DEFAULT_CONFIG) {
  return new Client().setEndpoint(endpoint).setProject(projectId);
}

export const client = createAppwriteClient();
