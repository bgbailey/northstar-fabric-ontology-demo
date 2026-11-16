// Renders an SVG diagram to PNG. Usage: node render.mjs <in.svg> <out.png> [density]
// Requires the "sharp" package (npm install sharp). Density 108 gives 1.5x the SVG size.
import fs from 'node:fs';
const sharp = (await import('sharp')).default;
const [,, src, dst, density] = process.argv;
if (!src || !dst) { console.error('usage: node render.mjs <in.svg> <out.png> [density]'); process.exit(2); }
const info = await sharp(fs.readFileSync(src), { density: Number(density || 108) }).png({ compressionLevel: 9 }).toFile(dst);
console.log(dst, info.width + 'x' + info.height, Math.round(info.size / 1024) + ' KB');
