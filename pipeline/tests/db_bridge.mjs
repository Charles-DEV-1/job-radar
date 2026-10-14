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
  CREATE SCHEMA auth;
  CREATE TABLE auth.users (id uuid PRIMARY KEY, email text, email_confirmed_at timestamptz);
  CREATE FUNCTION auth.uid() RETURNS uuid LANGUAGE sql STABLE AS
    $$ SELECT nullif(current_setting('request.jwt.claim.sub', true), '')::uuid $$;
  CREATE FUNCTION auth.jwt() RETURNS jsonb LANGUAGE sql STABLE AS
    $$ SELECT coalesce(nullif(current_setting('request.jwt.claims', true), ''), '{}')::jsonb $$;
  GRANT USAGE ON SCHEMA auth TO anon,authenticated,service_role;
  GRANT SELECT,INSERT,UPDATE,DELETE ON auth.users TO service_role;
  -- Supabase provides this schema; PGlite tests supply its relevant metadata
  -- tables so the real Stage 4 bucket migration and RLS policy can execute.
  CREATE SCHEMA storage;
  CREATE TABLE storage.buckets (
    id text PRIMARY KEY, name text NOT NULL, public boolean NOT NULL DEFAULT false,
    file_size_limit bigint, allowed_mime_types text[]
  );
  CREATE TABLE storage.objects (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(), bucket_id text REFERENCES storage.buckets(id)
  );
  ALTER TABLE storage.objects ENABLE ROW LEVEL SECURITY;
  GRANT USAGE ON SCHEMA storage TO anon, authenticated, service_role;
  GRANT SELECT,INSERT,UPDATE,DELETE ON storage.buckets,storage.objects TO service_role;
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
