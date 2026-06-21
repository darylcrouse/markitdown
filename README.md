# MarkItDown API

An authenticated HTTP service that converts uploaded documents to Markdown
using Microsoft MarkItDown. It is meant to be called from a case management
application. It is not meant to sit open on a public IP.

## Why it is built this way

This service will receive medical records, bills, and other client documents.
That is confidential material, and some of it is health information. So the
service requires an API key, runs over TLS through a proxy, limits file size
and type, times out long conversions, and disables third party plugins. An
open file parser on the public internet is a known target for malicious
uploads, so do not skip these controls.

## Run locally

    python3 -m venv .venv
    source .venv/bin/activate
    pip install -r requirements.txt
    export MARKITDOWN_API_KEYS="$(python3 -c 'import secrets;print(secrets.token_urlsafe(32))')"
    uvicorn app:app --host 127.0.0.1 --port 8000

Test it:

    curl -X POST http://127.0.0.1:8000/convert \
      -H "X-API-Key: YOUR_KEY" \
      -F "file=@some-record.pdf"

## Run with Docker

    docker build -t markitdown-api .
    docker run -d --name markitdown-api \
      --env-file .env \
      -p 127.0.0.1:8000:8000 \
      markitdown-api

Note the `127.0.0.1:8000` binding. The container listens on localhost only.
Public traffic reaches it through a reverse proxy, never directly.

## Exposing it to the public, the safe way

Do not bind the app itself to a public IP. Put a reverse proxy in front of it
that handles TLS, then forward to the app on localhost. This gives you a real
certificate, a single hardened entry point, and a place to add network level
rate limiting and IP rules.

Minimal Caddy example. Caddy gets a TLS certificate automatically:

    api.briefcasebliss.com {
        reverse_proxy 127.0.0.1:8000
    }

Nginx works too if you already run it. Terminate TLS at nginx and
`proxy_pass http://127.0.0.1:8000;`.

Easier options that avoid managing a server at all:

- A managed container host such as Google Cloud Run or Fly.io. Both give you
  HTTPS, scale to zero, and keep the box patched. Set the env vars in their
  dashboard. This pairs well since you already use Google services.
- Keep the app private and reach it through a Cloudflare Tunnel. Nothing
  listens on a public IP at all.

## Security checklist before you point your app at it

- API keys are long and random, stored as secrets, not in source.
- TLS is on. The app is only reachable over https.
- `ALLOWED_ORIGINS` is set to your app origin only.
- File size cap, type allowlist, and timeout are in place. They are on by default.
- Logs do not record document contents. The app already avoids this.
- Rotate keys by adding a second key, switching the app, then removing the old.

## Endpoints

`GET /health` returns service status. No key required.

`POST /convert` accepts a multipart form with field `file` and an optional
field `ocr`. Requires the `X-API-Key` header. Returns JSON:

    {
      "filename": "record.pdf",
      "title": null,
      "chars": 1234,
      "markdown": "# ..."
    }

`POST /convert/url` accepts JSON `{ "url": "...", "ocr": "auto" }` and the
`X-API-Key` header. It fetches the URL on the server and converts it. Returns
the same JSON shape. See the URL fetch safety notes below.

Errors use standard codes: 400 bad URL or rejected fetch, 401 bad or missing
key, 413 file too large, 415 unsupported type, 422 could not convert,
429 rate limited, 504 timeout.

## OCR for scanned documents

MarkItDown reads a PDF's existing text layer. A scanned, image only PDF has no
text layer, so it would convert to nothing. This service detects that and runs
OCR first.

The `ocr` field controls it:

- `auto` (default) OCRs a PDF only when it has little or no text. This is the
  setting you want for mixed intake.
- `force` OCRs every page even if text exists. Use it when a PDF has a bad or
  partial text layer.
- `off` never OCRs. Fastest, but scanned files come back empty.

Loose image files (png, jpg, tiff, and similar) are OCRed directly, since
MarkItDown does not OCR images on its own. OCR is slower than plain parsing,
which is why the default conversion timeout is 120 seconds. Raise
`CONVERT_TIMEOUT_SECONDS` for very large scans.

OCR needs three system tools: tesseract-ocr, ghostscript, and qpdf. The
Dockerfile installs them. If you run outside Docker, install them yourself.

## URL fetch safety

A server that fetches any URL is a Server Side Request Forgery risk. A caller
could aim it at a cloud metadata endpoint or an internal service and read
secrets. This service blocks private, loopback, link local, and reserved
addresses, blocks the cloud metadata address, rejects non http or https
schemes, revalidates every redirect hop, and caps the download size.

The strongest control is the allowlist. Set `URL_FETCH_ALLOWLIST` to the hosts
you actually pull documents from, such as your storage bucket or Drive. With
the allowlist set, the endpoint will only fetch from those hosts. IP blocking
alone cannot fully close DNS rebinding, so use the allowlist whenever you can.

## Calling it from the case management app

Plain fetch from your app backend. Keep the API key on the server side, not in
browser code, so it never ships to the client.

    async function toMarkdown(fileBlob, filename) {
      const form = new FormData();
      form.append("file", fileBlob, filename);

      const res = await fetch("https://api.briefcasebliss.com/convert", {
        method: "POST",
        headers: { "X-API-Key": process.env.MARKITDOWN_API_KEY },
        body: form,
      });

      if (!res.ok) {
        throw new Error("Convert failed: " + res.status);
      }
      const data = await res.json();
      return data.markdown;
    }

If your platform runs conversions from the browser, proxy the call through a
small server endpoint so the key stays hidden. Do not put the key in front end
code or in a URL.

To convert a stored file by URL instead of uploading bytes, post JSON:

    async function urlToMarkdown(fileUrl) {
      const res = await fetch("https://api.briefcasebliss.com/convert/url", {
        method: "POST",
        headers: {
          "X-API-Key": process.env.MARKITDOWN_API_KEY,
          "Content-Type": "application/json",
        },
        body: JSON.stringify({ url: fileUrl, ocr: "auto" }),
      });
      if (!res.ok) {
        throw new Error("Convert failed: " + res.status);
      }
      const data = await res.json();
      return data.markdown;
    }

Keep `URL_FETCH_ALLOWLIST` set to your storage host so this endpoint can only
reach the place your documents actually live.
