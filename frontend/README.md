# HAH frontend

React + TypeScript frontend for the HAH FastAPI application. Cloudflare Kumo provides the core UI components.

## Run locally

Install dependencies, then start the API and frontend together:

```bash
bun install
bun run dev
```

The API runs on port `8000`, the frontend runs on `5173`, and Vite proxies `/v1`
to the API. Use `bun run dev:frontend` or `bun run dev:backend` to run one side.

## Environment

Copy `.env.example` to `.env` when the frontend and API are on different origins:

- `VITE_API_BASE_URL`: public FastAPI origin. Leave blank for same-origin requests.
- `VITE_MCP_URL`: MCP URL copied from the landing page.

## Checks

```bash
bun run lint
bun run build
```
