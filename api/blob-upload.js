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
// Without it, `handleUpload` throws and we return a 400 the client can
// surface.
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

export default async function handler(request, response) {
  if (request.method !== "POST") {
    response.status(405).json({ error: "method not allowed" });
    return;
  }

  // Vercel's Node runtime auto-parses JSON bodies when Content-Type is
  // application/json, which is what `@vercel/blob/client`'s upload()
  // sends for the handshake. `handleUpload` accepts the already-parsed
  // object here — no re-reading the stream required.
  const body = request.body;
  if (!body || typeof body !== "object") {
    response.status(400).json({ error: "invalid JSON body" });
    return;
  }

  try {
    const jsonResponse = await handleUpload({
      body,
      request,
      onBeforeGenerateToken: async (pathname /*, clientPayload */) => {
        // This is a public converter — no user sessions to check. Protection
        // is reduced to: tight content-type allowlist, hard size cap, and
        // `addRandomSuffix` so pathnames can't be guessed or collided.
        // The token SDK applies a sensible default TTL (~30 min) which
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
    const message = error && error.message ? error.message : String(error);
    // BLOB_READ_WRITE_TOKEN missing or invalid is the most common cause —
    // surface it verbatim so the frontend can tell the user to set up Blob.
    response.status(400).json({ error: message });
  }
}
