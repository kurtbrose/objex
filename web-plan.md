# Objex Web Plan

## Goal

Build a local, read-only browser UI for exploring an objex analysis database without
pulling in a web framework or a frontend build toolchain.

The app should:

- run over `localhost`
- open a single SQLite analysis DB
- serve one HTML page plus a small JS/CSS bundle
- expose a handful of JSON endpoints shaped for the UI
- reuse `Reader` instead of recreating query logic

## Non-goals

- no auth or multi-user support
- no write operations
- no OpenAPI / schema framework
- no server-side templating
- no graph canvas in the first pass
- no external JS framework

## Architecture

### Backend

- stdlib `http.server`
- one request handler
- a few JSON endpoints
- static responses for `index.html`, `app.js`, and `styles.css`

### Frontend

- one page shell
- one JS file that fetches JSON and updates the DOM
- one CSS file for layout and readability

## Initial CLI

Add:

```bash
python -m objex web analysis.db
python -m objex web analysis.db --port 8080
python -m objex web analysis.db --host 127.0.0.1
```

Possible later:

- `--open`
- `--bind 0.0.0.0` if remote access is ever desired

## First Endpoints

- `GET /`
  - serves the main HTML shell
- `GET /app.js`
- `GET /styles.css`
- `GET /api/summary`
- `GET /api/object?id=<obj_id>`
- `GET /api/referents?id=<obj_id>&limit=<n>`
- `GET /api/referrers?id=<obj_id>&limit=<n>`
- `GET /api/random`
- `GET /api/type-search?q=<pattern>&limit=<n>`
- `GET /api/go?path=<object-path>`

Likely second wave:

- `GET /api/path-to-module?id=<obj_id>`
- `GET /api/path-to-frame?id=<obj_id>`
- `GET /api/top-types`
- `GET /api/largest-objects`

## Reader Additions

Add helper methods that return UI-shaped data:

- `summary_stats()`
- `object_label(obj_id)`
- `object_summary(obj_id)`
- `object_referents_data(obj_id, limit=...)`
- `object_referrers_data(obj_id, limit=...)`
- `type_search_data(query, limit=...)`
- `random_object_id()`
- `resolve_path(path)`

Keep the JSON contract stable at the web layer instead of making the frontend
assemble meaning from many tiny primitive calls.

## Frontend Layout

### Top Bar

- object ID jump form
- path jump form
- type search form
- random button
- home button

### Main Layout

- left: current object summary
- middle: outbound references
- right: inbound references

### Secondary Panels

- DB summary strip
- search results panel
- message/error area

## Navigation Model

All navigation should converge on a single action:

- load object by id

Sources of navigation:

- object click
- random object
- direct ID input
- path lookup
- type search result click

Browser history should store the current object id in the URL query string.

## First Milestone

Ship a useful but narrow browser:

- open the DB
- show DB summary
- load an object by ID
- click inbound/outbound refs
- jump to a random object
- search for types

This should already beat the current REPL for discoverability.

## Follow-up Milestones

### Milestone 2

- module/frame root path views
- better breadcrumbs/history
- top types and largest objects

### Milestone 3

- object marks
- frame stack display
- richer object-specific panels

## Constraints

- no external backend dependencies
- no external frontend dependencies
- keep the first implementation small enough to stay maintainable
