// static_page_server.mjs <directory> <port>: serves a static_page target's files
// (bazel/static_page.bzl) on 127.0.0.1, for `bazel run`.

import { readFile, stat } from "node:fs/promises";
import { createServer } from "node:http";
import { extname, join, resolve, sep } from "node:path";

const [directory, port] = process.argv.slice(2);
const root = resolve(directory);

const types = {
  ".css": "text/css",
  ".html": "text/html",
  ".js": "text/javascript",
  ".json": "application/json",
  ".svg": "image/svg+xml",
};

/** The file a request path names under root, or undefined if it names none. */
async function fileFor(pathname) {
  let path = resolve(join(root, decodeURIComponent(pathname)));
  if (path !== root && !path.startsWith(root + sep)) return undefined;
  try {
    if ((await stat(path)).isDirectory()) path = join(path, "index.html");
    await stat(path);
    return path;
  } catch {
    return undefined;
  }
}

createServer(async (request, response) => {
  const path = await fileFor(new URL(request.url, "http://localhost").pathname);
  if (!path) {
    response.writeHead(404).end();
    return;
  }
  response.writeHead(200, {
    "Content-Type": types[extname(path)] ?? "text/plain",
    // Every load sees the latest `bazel run`.
    "Cache-Control": "no-cache",
  });
  response.end(await readFile(path));
}).listen(Number(port), "127.0.0.1", () => {
  console.log(`Serving http://127.0.0.1:${port}/`);
});
