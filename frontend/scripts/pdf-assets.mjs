import { cp, mkdir } from 'node:fs/promises';
import { createRequire } from 'node:module';
import { dirname, resolve } from 'node:path';

const require = createRequire(import.meta.url);
const source = dirname(require.resolve('pdfjs-dist/package.json'));
for (const name of ['cmaps', 'standard_fonts', 'wasm']) {
  const target = resolve('public/pdfjs', name);
  await mkdir(target, { recursive: true });
  await cp(resolve(source, name), target, { recursive: true });
}
