// Client-upload token endpoint for Vercel Blob.
//
// Exists to circumvent Vercel's 4.5 MB request-body limit on serverless
// functions: instead of streaming a 14 MB .apkg through /api/convert (which
// Vercel would reject at the edge with a 413 before our handler runs), the
// browser asks *this* endpoint for a short-lived signed token and then PUTs
// the file **directly** to Vercel Blob's object storage. The conversion
// function then fetches the blob by URL — a function outbound fetch has no
// size cap.
//
// Runs on the default Node.js runtime. We originally tried the Edge
// runtime for faster cold starts, but `@vercel/blob/client`'s
// `handleUpload` pulls in undici + node: built-ins (stream, crypto, tls,
// …) which Edge Functions disallow, so deployment failed with
// "unsupported modules". Node runtime has no such restriction. Cold-start
// cost is a non-issue here: this endpoint is called once per upload, for
// a handshake that's much cheaper than the conversion itself.
//
// Setup requirement: the Vercel project must have a Blob store connected
// (Project → Storage → Create → Blob → Connect). That provisioning
// auto-injects `BLOB_READ_WRITE_TOKEN` into the function environment.
// Without it, `handleUpload` throws "No token found" and we return a 400
// with that message intact.
import { handleUpload } from "@vercel/blob/client";

// The Anki → Slides pipeline accepts .apkg (SQLite bundle), .zip (txt + media),
// and .txt (plain export). Allowing the generic octet-stream covers browsers
// and OSes that don't attach a MIME type to .apkg. We cap uploads at 100 MB
// so someone can't dump a 5 GB .colpkg through our token.
const ALLOWED_CONTENT_TYPES = [
  "application/octet-stream",
  "application/zip",
  "application/x-zip-compressed",
  "text/plain",
];
const MAX_UPLOAD_BYTES = 100 * 1024 * 1024;

// Defensive body reader. Vercel's Node runtime auto-parses JSON bodies for
// Content-Type: application/json, so request.body is usually an object —
// but if the incoming header is missing or non-standard we fall back to
// reading from the raw stream so handleUpload still gets what it needs.
async function parseBody(request) {
  const b = request.body;
  if (b && typeof b === "object" && !Buffer.isBuffer(b)) return b;
  if (typeof b === "string") return JSON.parse(b);

  const chunks = [];
  for await (const chunk of request) {
    chunks.push(typeof chunk === "string" ? Buffer.from(chunk) : chunk);
  }
  const raw = Buffer.concat(chunks).toString("utf8");
  return raw ? JSON.parse(raw) : {};
}

export default async function handler(request, response) {
  if (request.method !== "POST") {
    response.status(405).json({ error: "method not allowed" });
    return;
  }

  // Anything logged here shows up in Vercel's function logs
  // (Project → Observability / Logs → filter by /api/blob-upload).
  // We only log presence booleans, never the token itself.
  console.log("blob-upload: request received", {
    method: request.method,
    contentType: request.headers["content-type"],
    hasToken: Boolean(process.env.BLOB_READ_WRITE_TOKEN),
  });

  let body;
  try {
    body = await parseBody(request);
  } catch (err) {
    console.error("blob-upload: body parse failed", err);
    response.status(400).json({
      error: "invalid JSON body",
      reason: err && err.message ? err.message : String(err),
    });
    return;
  }

  console.log("blob-upload: body parsed", {
    type: body && body.type,
    hasPayload: Boolean(body && body.payload),
  });

  try {
    const jsonResponse = await handleUpload({
      body,
      request,
      onBeforeGenerateToken: async (pathname /*, clientPayload */) => {
        // This is a public converter — no user sessions to check. Protection
        // is reduced to: tight content-type allowlist, hard size cap, and
        // `addRandomSuffix` so pathnames can't be guessed or collided.
        // The token SDK applies a sensible default TTL (~1 hour) which
        // is plenty for our 60 s convert function.
        return {
          allowedContentTypes: ALLOWED_CONTENT_TYPES,
          addRandomSuffix: true,
          maximumSizeInBytes: MAX_UPLOAD_BYTES,
        };
      },
      onUploadCompleted: async () => {
        // We don't need the webhook — /api/convert will fetch the blob by
        // URL inline. Left as a no-op so Vercel doesn't retry the webhook.
      },
    });
    response.status(200).json(jsonResponse);
  } catch (error) {
    // Full error (with stack) goes to Vercel function logs; a condensed
    // version goes back in the response body so we can inspect it from
    // the browser's Network tab even without dashboard access.
    console.error("blob-upload: handleUpload threw", error);
    const message = error && error.message ? error.message : String(error);
    response.status(400).json({
      error: message,
      name: error && error.name ? error.name : undefined,
    });
  }
}
