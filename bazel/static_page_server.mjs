// static_page_server.mjs <directory> <port> [port]: serves a static_page target's files
// (bazel/static_page.bzl) on 127.0.0.1, for `bazel run`. The target passes the first two; a port
// given after `bazel run <target> --` overrides its default.

import { readFile, stat } from "node:fs/promises";
import { createServer } from "node:http";
import { extname, join, resolve, sep } from "node:path";

const [directory, defaultPort, port = defaultPort] = process.argv.slice(2);
const root = resolve(directory);
// Off a laptop (a container behind an ingress), HOST=0.0.0.0 makes the page reachable, and
// BASE_PATH (e.g. /studio) serves it under that path prefix of a shared host.
const host = process.env.HOST ?? "127.0.0.1";
const basePath = (process.env.BASE_PATH ?? "").replace(/\/+$/, "");

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
  let pathname = new URL(request.url, "http://localhost").pathname;
  if (request.url === "/healthz") {
    response.writeHead(200).end("ok");
    return;
  }
  if (basePath) {
    if (pathname === basePath) {
      // Relative URLs in index.html resolve against the directory, so add its slash.
      response.writeHead(308, { Location: basePath + "/" }).end();
      return;
    }
    if (!pathname.startsWith(basePath + "/")) {
      response.writeHead(404).end();
      return;
    }
    pathname = pathname.slice(basePath.length);
  }
  const path = await fileFor(pathname);
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
}).listen(Number(port), host, () => {
  console.log(`Serving http://${host}:${port}${basePath}/`);
});
