import { Account, Functions, Databases, TablesDB, Query, ExecutionMethod } from 'appwrite';
import { client as defaultClient, createAppwriteClient } from './appwrite.js';
import { DEFAULT_CONFIG, validateConfig, normalizeDashboard } from './data.js';

export function createService(input) {
  const config = validateConfig(input);
  const client = config.endpoint === DEFAULT_CONFIG.endpoint && config.projectId === DEFAULT_CONFIG.projectId
    ? defaultClient : createAppwriteClient(config);
  const account = new Account(client);
  const functions = new Functions(client);
  const database = config.storage === 'tables' ? new TablesDB(client) : new Databases(client);
  const list = async (databaseId, id, queries) => {
    if (config.storage === 'tables') {
      return (await database.listRows({ databaseId, tableId: id, queries })).rows;
    }
    return (await database.listDocuments({ databaseId, collectionId: id, queries })).documents;
  };
  return {
    config,
    ping: () => client.ping(),
    account: () => account.get(),
    signIn: (email, password) => account.createEmailPasswordSession({ email, password }),
    signOut: () => account.deleteSession({ sessionId: 'current' }),
    async dashboard(days) {
      const cutoff = new Date(Date.now() - days * 86400000).toISOString();
      const stateRequest = list(config.stateDatabaseId || config.databaseId, config.stateId, [Query.equal('state_key', ['global']), Query.limit(1)]);
      const logsRequest = (async () => {
        const rows = [];
        let cursor;
        let truncated = false;
        // A visible cap bounds browser/network work on large installations.
        for (let page = 0; page < 20; page++) {
          const queries = [Query.orderDesc('$createdAt'), Query.greaterThanEqual('$createdAt', cutoff), Query.limit(100)];
          if (cursor) queries.push(Query.cursorAfter(cursor));
          const batch = await list(config.databaseId, config.logsId, queries);
          rows.push(...batch);
          if (batch.length < 100) break;
          cursor = batch.at(-1).$id;
          if (page === 19) truncated = true;
        }
        return { rows, truncated };
      })();
      const [stateRows, logs] = await Promise.all([stateRequest, logsRequest]);
      return { ...normalizeDashboard(stateRows, logs.rows), truncated: logs.truncated };
    },
    executions: () => functions.listExecutions({ functionId: config.functionId, queries: [Query.orderDesc('$createdAt'), Query.limit(25)] }),
    start: () => functions.createExecution({ functionId: config.functionId, async: true, body: JSON.stringify({ source: 'relayroom' }), xpath: '/', method: ExecutionMethod.POST }),
    execution: (executionId) => functions.getExecution({ functionId: config.functionId, executionId }),
  };
}
