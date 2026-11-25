# multidial web — 多拨监控面板

Vite + React + TypeScript + Tailwind + shadcn/ui + Framer Motion. Built to a
**static bundle** embedded inside a LuCI page (LuCI login gates access).

## Build

```sh
npm install
npm run build      # -> dist/multidial.js + dist/multidial.css (fixed names, no hash)
```

Deploy `dist/multidial.js` and `dist/multidial.css` to the router at
`/www/luci-static/multidial/`. The LuCI template
(`multidial/luci/view/multidial/dashboard.htm`) references them and provides the
`#multidial-root` mount node + `window.__MULTIDIAL_API__`.

## Dev

```sh
npm run dev        # serves index.html; set window.__MULTIDIAL_API__ or proxy /api
```

## API

The app reads `window.__MULTIDIAL_API__` (default `/api`) and calls:

- `GET  ${API}/status` → snapshot
- `POST ${API}/reauth` → snapshot (force re-auth all dials)
- `POST ${API}/reload` → snapshot (re-read config + reconcile)

On the router the LuCI controller proxies these same-origin to the Python daemon
on `127.0.0.1:23334`.

> Security: the UI only ever renders `account_userid` (e.g. `demo_user_a`); it never
> references a `userName`.
