// Cache the exact Auth UI assets during image build. Tests stay offline.
import {createHash} from "node:crypto";
import {mkdir, readFile, writeFile} from "node:fs/promises";

const assets = JSON.parse(await readFile(new URL("./preview-assets.json", import.meta.url)));
const directory = new URL("./preview-assets/", import.meta.url);
await mkdir(directory, {recursive: true});
for (const asset of assets) {
  const url = new URL(asset.url);
  if (url.origin !== "https://cdnjs.cloudflare.com" || !/^[a-zA-Z0-9_.-]+$/.test(asset.file)) {
    throw new Error("Unsafe preview asset path");
  }
  const response = await fetch(url, {signal: AbortSignal.timeout(30_000)});
  if (!response.ok) throw new Error(`Preview asset download failed: ${response.status}`);
  const body = Buffer.from(await response.arrayBuffer());
  if (body.length > 1_048_576 || (asset.size !== undefined && body.length !== asset.size)
      || createHash("sha512").update(body).digest("base64") !== asset.sha512) {
    throw new Error(`Preview asset checksum mismatch: ${asset.file}`);
  }
  await writeFile(new URL(asset.file, directory), body);
}
