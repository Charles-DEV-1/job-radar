// @ts-check
// Real PostgreSQL/PLpgSQL test engine. No network endpoints or production data.
import { createInterface } from 'node:readline';
import { pathToFileURL } from 'node:url';
import { resolve } from 'node:path';
import { readFile } from 'node:fs/promises';

const modulePath = resolve(process.argv[2], 'node_modules/@electric-sql/pglite/dist/index.js');
const { PGlite } = await import(pathToFileURL(modulePath));
const db = new PGlite();
await db.waitReady;
await db.exec(`
  CREATE ROLE anon;
  CREATE ROLE authenticated;
  CREATE ROLE service_role BYPASSRLS;
  GRANT USAGE ON SCHEMA public TO anon, authenticated, service_role;
`);
for (const migration of process.argv.slice(3)) {
  await db.exec(await readFile(migration, 'utf8'));
}
process.stdout.write(JSON.stringify({ ready: true }) + '\n');
const lines = createInterface({ input: process.stdin, crlfDelay: Infinity });
/** @type {Promise<void>} */
let queue = Promise.resolve();
lines.on('line', line => {
  queue = queue.then(async () => {
    try {
      /** @type {{sql: string, params?: unknown[], exec?: boolean}} */
      const request = JSON.parse(line);
      const result = request.exec
        ? await db.exec(request.sql)
        : await db.query(request.sql, request.params ?? []);
      process.stdout.write(JSON.stringify({ result }) + '\n');
    } catch (error) {
      const failure = /** @type {Error & {code?: string}} */ (error);
      process.stdout.write(JSON.stringify({ error: failure.message, code: failure.code }) + '\n');
    }
  });
});
lines.on('close', async () => { await queue; await db.close(); });
