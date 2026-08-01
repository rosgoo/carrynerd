/* GET/POST /api/internal/domains — the read/write half of the domain review
 * page. DEV ONLY: it reads and rewrites data/domains-todo.csv on the local
 * disk, which only exists (and is only writable) on a dev checkout. In any
 * built deployment both verbs answer 404 before touching anything.
 *
 * POST replaces the whole file from the rows the page sends, written
 * atomically (tmp + rename) so an interrupted save cannot half-eat the list.
 * The platform column comes from `resolve_domains.py --detect-platforms`; the
 * page treats it as read-only context and sends it back untouched.
 */

import type { APIRoute } from 'astro';
import { readFile, writeFile, rename } from 'node:fs/promises';
import path from 'node:path';

export const prerender = false;

const CSV = path.join(process.cwd(), 'data', 'domains-todo.csv');
const FIELDS = ['name', 'domain', 'platform', 'best_guess', 'why_unsure', 'search'];

const json = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), {
    status,
    headers: { 'content-type': 'application/json' },
  });

const gone = () => new Response('Not found', { status: 404 });

/* Quote-aware CSV — why_unsure carries commas and parentheses, so naive
 * split(',') corrupts exactly the rows a person most needs to read. */
function parseCsv(text: string): string[][] {
  const rows: string[][] = [];
  let row: string[] = [], field = '', quoted = false;
  for (let i = 0; i < text.length; i++) {
    const c = text[i];
    if (quoted) {
      if (c === '"' && text[i + 1] === '"') { field += '"'; i++; }
      else if (c === '"') quoted = false;
      else field += c;
    } else if (c === '"') quoted = true;
    else if (c === ',') { row.push(field); field = ''; }
    else if (c === '\n' || c === '\r') {
      if (c === '\r' && text[i + 1] === '\n') i++;
      row.push(field); field = '';
      if (row.length > 1 || row[0] !== '') rows.push(row);
      row = [];
    } else field += c;
  }
  if (field !== '' || row.length) { row.push(field); rows.push(row); }
  return rows;
}

const cell = (s: string) =>
  /[",\n\r]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s;

export const GET: APIRoute = async () => {
  if (!import.meta.env.DEV) return gone();
  let text: string;
  try {
    text = await readFile(CSV, 'utf-8');
  } catch {
    return json({ error: 'data/domains-todo.csv not found — run resolve_domains.py first' }, 404);
  }
  const [header, ...body] = parseCsv(text);
  const idx = Object.fromEntries(header.map((h, i) => [h.trim(), i]));
  const rows = body.map((r) =>
    Object.fromEntries(FIELDS.map((f) => [f, idx[f] != null ? (r[idx[f]] ?? '') : ''])),
  );
  return json({ rows });
};

export const POST: APIRoute = async ({ request }) => {
  if (!import.meta.env.DEV) return gone();
  let rows: Record<string, string>[];
  try {
    rows = (await request.json()).rows;
    if (!Array.isArray(rows) || !rows.length) throw new Error('empty');
    for (const r of rows) if (typeof r.name !== 'string' || !r.name) throw new Error('row without name');
  } catch (e) {
    return json({ error: `bad payload: ${e}` }, 400);
  }
  const out = [FIELDS.join(',')]
    .concat(rows.map((r) => FIELDS.map((f) => cell(String(r[f] ?? ''))).join(',')))
    .join('\n') + '\n';
  const tmp = CSV + '.tmp';
  await writeFile(tmp, out, 'utf-8');
  await rename(tmp, CSV);
  return json({ saved: rows.length });
};
