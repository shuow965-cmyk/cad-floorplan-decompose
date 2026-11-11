#!/usr/bin/env node
// Convert a local DWG to DXF via @mlightcad/libredwg-web.
// Does not depend on the current working directory. Never overwrites the source DWG.
//
//   node /path/to/dwg2dxf.mjs input.dwg output.dxf
//
// Package lookup order:
//   1. $LIBREDWG_WEB (directory of the @mlightcad/libredwg-web package)
//   2. /workspace/cad-skill-pilot/node_modules/@mlightcad/libredwg-web
//   3. ./node_modules/@mlightcad/libredwg-web relative to this script (unusual)
//   4. ./node_modules/@mlightcad/libredwg-web relative to cwd (last resort)

import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';

const here = path.dirname(fileURLToPath(import.meta.url));
const cwd = process.cwd();
const pkgName = '@mlightcad/libredwg-web';

function candidates() {
  const out = [];
  if (process.env.LIBREDWG_WEB) out.push(process.env.LIBREDWG_WEB);
  out.push('/workspace/cad-skill-pilot/node_modules/' + pkgName);
  out.push(path.join(here, 'node_modules', pkgName));
  out.push(path.join(cwd, 'node_modules', pkgName));
  return out;
}

function resolvePkg() {
  for (const root of candidates()) {
    const js = path.join(root, 'dist', 'libredwg-web.js');
    const wasm = path.join(root, 'wasm');
    if (fs.existsSync(js) && fs.existsSync(wasm)) {
      return { root, js, wasm };
    }
  }
  throw new Error(
    'Cannot find @mlightcad/libredwg-web. Set LIBREDWG_WEB to the package directory, ' +
    'or install it under /workspace/cad-skill-pilot/node_modules/. Tried:\n' +
    candidates().join('\n')
  );
}

const src = process.argv[2];
const dst = process.argv[3];
if (!src || !dst) {
  console.error('Usage: node dwg2dxf.mjs input.dwg output.dxf');
  process.exit(2);
}
if (!fs.existsSync(src)) {
  console.error('Source DWG not found:', src);
  process.exit(1);
}

const loc = resolvePkg();
const { LibreDwg } = await import(pathToFileURL(loc.js).href);
const l = await LibreDwg.create(loc.wasm);
const b = fs.readFileSync(src);
const d = l.dwg_write_dxf(b.buffer.slice(b.byteOffset, b.byteOffset + b.byteLength));
if (!d) throw new Error('DXF export returned null');
fs.mkdirSync(path.dirname(path.resolve(dst)), { recursive: true });
fs.writeFileSync(dst, d);
const n = typeof d === 'string' ? d.length : d.byteLength;
console.log('DXF_BYTES', n, '->', dst, '(via', loc.root + ')');
