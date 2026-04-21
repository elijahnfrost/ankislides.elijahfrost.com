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
// Runs on the Edge runtime because `handleUpload` is designed around the
// Web API `Request` object, and the Edge runtime gives us cold starts
// measured in tens of ms without needing to spin up Python/Node.
//
// Setup requirement: the Vercel project must have a Blob store connected
// (Project → Storage → Create → Blob → Connect). That provisioning
// auto-injects `BLOB_READ_WRITE_TOKEN` into the function environment.
// Without it, `handleUpload` throws and we return a 400 the client can
// surface.
import { handleUpload } from "@vercel/blob/client";

export const config = { runtime: "edge" };

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

export default async function handler(request) {
  if (request.method !== "POST") {
    return Response.json({ error: "method not allowed" }, { status: 405 });
  }

  let body;
  try {
    body = await request.json();
  } catch (_) {
    return Response.json({ error: "invalid JSON body" }, { status: 400 });
  }

  try {
    const jsonResponse = await handleUpload({
      body,
      request,
      onBeforeGenerateToken: async (pathname /*, clientPayload */) => {
        // This is a public converter — no user sessions to check. Protection
        // is reduced to: tight content-type allowlist, hard size cap, and
        // `addRandomSuffix` so pathnames can't be guessed or collided.
        // Returned options are documented on
        // https://vercel.com/docs/vercel-blob/using-blob-sdk#onbeforegeneratetoken
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
    return Response.json(jsonResponse);
  } catch (error) {
    const message = error && error.message ? error.message : String(error);
    // BLOB_READ_WRITE_TOKEN missing or invalid is the most common cause —
    // surface it verbatim so the frontend can tell the user to set up Blob.
    return Response.json({ error: message }, { status: 400 });
  }
}
