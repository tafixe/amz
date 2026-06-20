// Standalone worker that serves the Amazon links dashboard from its own R2
// bucket.
//
// Access is gated by a password. The password is NEVER stored in plaintext:
// only its SHA-256 hash lives here, and the check runs server-side, so even the
// data files (/data/*.json) can't be read without logging in. A session cookie
// (cleared when the browser closes) means the password is asked once per
// browser session.

const MIME = {
  html: "text/html; charset=utf-8",
  json: "application/json",
  ico: "image/x-icon",
  txt: "text/plain",
  svg: "image/svg+xml",
};

const LINKS_KEY = "data/amazon_links.json";
const CLEARED_KEY = "data/cleared.json";
const MAX_CLEARED = 20000;

// The access password lives ONLY in a Cloudflare secret (env.ADMIN_PASSWORD),
// never in this code. The session cookie token is derived from that secret, so
// it cannot be forged by anyone reading this public source.
async function sha256hex(str) {
  const buf = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(str));
  return [...new Uint8Array(buf)].map((b) => b.toString(16).padStart(2, "0")).join("");
}

const LOGIN_HTML = `<!DOCTYPE html>
<html lang="pt">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Entrar</title>
<link rel="icon" href="data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100'><text y='.9em' font-size='90'>\u{1F512}</text></svg>">
<style>
  :root { --bg:#121212; --card:#1a1a1a; --border:#2a2a2a; --text:#ededed;
    --muted:#8a9099; --brand:#ff9900; --green:#25d366; }
  * { margin:0; padding:0; box-sizing:border-box; }
  body { font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;
    background:var(--bg); color:var(--text); min-height:100vh; display:flex;
    flex-direction:column; align-items:center; justify-content:center; gap:22px; padding:24px; }
  .card { background:var(--card); border:1px solid var(--border); border-radius:16px;
    padding:28px 22px; width:100%; max-width:380px; text-align:center; }
  h1 { font-size:20px; margin-bottom:18px; }
  input { width:100%; padding:13px 14px; border-radius:10px; border:1px solid var(--border);
    background:var(--bg); color:var(--text); font-size:16px; outline:none; }
  input:focus { border-color:var(--brand); }
  button { width:100%; margin-top:14px; padding:13px; border:0; border-radius:10px;
    background:var(--brand); color:#1a1a1a; font-size:16px; font-weight:700; cursor:pointer; }
  .err { color:#ef4444; font-size:13px; min-height:16px; margin-top:10px; }
  .big { width:100%; max-width:380px; display:block; text-align:center; text-decoration:none;
    background:var(--green); color:#fff; font-size:22px; font-weight:800; padding:26px 18px;
    border-radius:16px; box-shadow:0 6px 20px rgba(37,211,102,.25); }
  .big:active { transform:scale(.99); }
</style>
</head>
<body>
  <div class="card">
    <h1>\u{1F512} Acesso privado</h1>
    <input id="pw" type="password" placeholder="Palavra-passe" autofocus
           onkeydown="if(event.key==='Enter')entrar()">
    <button onclick="entrar()">Entrar</button>
    <div class="err" id="err"></div>
  </div>
  <a class="big" href="__PROMO_URL__" target="_blank" rel="noopener">Vê os descontos aqui</a>
<script>
async function entrar(){
  const pw = document.getElementById("pw").value;
  const err = document.getElementById("err");
  err.textContent = "";
  try {
    const r = await fetch("/api/login", {
      method:"POST", credentials:"same-origin",
      headers:{ "Content-Type":"application/json" },
      body: JSON.stringify({ password: pw })
    });
    if (r.ok) { location.reload(); }
    else { err.textContent = "Palavra-passe errada."; }
  } catch(e) { err.textContent = "Erro de ligacao."; }
}
</script>
</body>
</html>`;

export default {
  // Cron trigger (reliable, every 10 min) — GitHub's own cron is throttled, so
  // the worker dispatches the scan workflow via the GitHub API instead. Token
  // and repo come from secrets, so nothing identifying lives in this code.
  async scheduled(event, env, ctx) {
    if (!env.GH_DISPATCH_TOKEN || !env.GH_REPO) return;
    const wf = env.GH_WORKFLOW || "amazon.yml";
    ctx.waitUntil(fetch(
      `https://api.github.com/repos/${env.GH_REPO}/actions/workflows/${wf}/dispatches`,
      {
        method: "POST",
        headers: {
          "Authorization": `Bearer ${env.GH_DISPATCH_TOKEN}`,
          "Accept": "application/vnd.github+json",
          "User-Agent": "deals-cron",
          "Content-Type": "application/json",
        },
        body: JSON.stringify({ ref: "main" }),
      },
    ));
  },

  async fetch(request, env) {
    const url = new URL(request.url);

    // Redirect www → apex (host-agnostic, so no domain is named here)
    if (url.hostname.startsWith("www.")) {
      url.hostname = url.hostname.slice(4);
      return Response.redirect(url.toString(), 301);
    }

    const cors = {
      "Access-Control-Allow-Origin": "*",
      "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
      "Access-Control-Allow-Headers": "Content-Type",
    };

    if (request.method === "OPTIONS") {
      return new Response(null, { headers: cors });
    }

    // Session token derived from the server-only password secret. With no
    // ADMIN_PASSWORD configured the site is locked (fail closed).
    const adminPw = env.ADMIN_PASSWORD || "";
    const token = adminPw ? await sha256hex(adminPw + "|session-v1") : "";

    // Login: verify the password against the secret, then set a session cookie.
    if (request.method === "POST" && url.pathname === "/api/login") {
      let pw = "";
      try { pw = (await request.json()).password || ""; } catch (e) {}
      if (adminPw && pw === adminPw) {
        return new Response(JSON.stringify({ ok: true }), {
          headers: {
            "Content-Type": "application/json",
            "Set-Cookie": `auth=${token}; HttpOnly; Secure; SameSite=Lax; Path=/`,
          },
        });
      }
      return new Response(JSON.stringify({ error: "errada" }), {
        status: 401, headers: { "Content-Type": "application/json" },
      });
    }

    // Edge proxies for two upstream APIs that block the GitHub Actions IP. The
    // upstream URL templates live in secrets (env.PROXY_A_URL / env.PROXY_B_URL,
    // each with a {page} placeholder) so no source site is named here. Each is
    // restricted to its one configured upstream — not an open proxy. No auth.
    const proxyMap = { "/api/src-a": env.PROXY_A_URL, "/api/src-b": env.PROXY_B_URL };
    if (request.method === "GET" && proxyMap[url.pathname]) {
      const tmpl = proxyMap[url.pathname];
      const page = (url.searchParams.get("page") || "1").replace(/[^0-9]/g, "") || "1";
      try {
        const up = await fetch(tmpl.replace("{page}", page), {
          headers: { "User-Agent": "Mozilla/5.0", "Accept": "application/json" },
        });
        return new Response(up.body, {
          status: up.status,
          headers: { "Content-Type": "application/json", "Access-Control-Allow-Origin": "*" },
        });
      } catch (e) {
        return new Response("[]", { status: 502, headers: { "Content-Type": "application/json" } });
      }
    }

    // Gate everything else behind the session cookie.
    const cookies = request.headers.get("Cookie") || "";
    const authed = !!token && cookies.split(/;\s*/).includes(`auth=${token}`);
    if (!authed) {
      if (url.pathname.startsWith("/data/") || url.pathname.startsWith("/api/")) {
        return new Response("Unauthorized", { status: 401, headers: cors });
      }
      return new Response(LOGIN_HTML.replace("__PROMO_URL__", env.PROMO_URL || "#"), {
        headers: { "Content-Type": "text/html; charset=utf-8", "Cache-Control": "no-store" },
      });
    }

    // POST /api/hide — hide one or more links for ALL devices (server-side).
    // Appends to the same cleared set the scraper already excludes, so an
    // opened/hidden deal disappears everywhere and never comes back.
    if (request.method === "POST" && url.pathname === "/api/hide") {
      let urls = [];
      try { const b = await request.json(); urls = b.urls || (b.url ? [b.url] : []); } catch (e) {}
      urls = urls.filter((u) => typeof u === "string" && u);
      if (urls.length) {
        let cleared = [];
        const cl = await env.BUCKET.get(CLEARED_KEY);
        if (cl) { try { cleared = await cl.json(); } catch (e) {} }
        cleared = [...new Set([...cleared, ...urls])].slice(-MAX_CLEARED);
        await env.BUCKET.put(CLEARED_KEY, JSON.stringify(cleared), {
          httpMetadata: { contentType: "application/json" },
        });
      }
      return Response.json({ ok: true, hidden: urls.length }, { headers: cors });
    }

    // POST /api/clear — hide every current Telegram link for all devices.
    if (request.method === "POST" && url.pathname === "/api/clear") {
      let data = { links: [] };
      const cur = await env.BUCKET.get(LINKS_KEY);
      if (cur) { try { data = await cur.json(); } catch (e) {} }
      const urls = (data.links || []).map((l) => l.url).filter(Boolean);

      let cleared = [];
      const cl = await env.BUCKET.get(CLEARED_KEY);
      if (cl) { try { cleared = await cl.json(); } catch (e) {} }
      cleared = [...new Set([...cleared, ...urls])].slice(-MAX_CLEARED);
      await env.BUCKET.put(CLEARED_KEY, JSON.stringify(cleared), {
        httpMetadata: { contentType: "application/json" },
      });

      data.links = [];
      await env.BUCKET.put(LINKS_KEY, JSON.stringify(data, null, 2), {
        httpMetadata: { contentType: "application/json" },
      });

      return Response.json({ ok: true, cleared: urls.length }, { headers: cors });
    }

    if (request.method !== "GET" && request.method !== "HEAD") {
      return new Response("Method Not Allowed", { status: 405, headers: cors });
    }

    let key = decodeURIComponent(url.pathname.slice(1));
    if (!key) key = "index.html";

    const obj = await env.BUCKET.get(key);
    if (!obj) {
      return new Response("Not Found", { status: 404, headers: cors });
    }

    const ext = key.split(".").pop().toLowerCase();
    const headers = new Headers(cors);
    headers.set("Content-Type", obj.httpMetadata?.contentType || MIME[ext] || "application/octet-stream");
    headers.set("Content-Length", obj.size);
    headers.set("Cache-Control", key.endsWith(".html") ? "no-cache, no-store, must-revalidate" : "public, max-age=60");

    if (request.method === "HEAD") return new Response(null, { headers });
    return new Response(obj.body, { headers });
  },
};
