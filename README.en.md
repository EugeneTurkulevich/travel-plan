# travel-plan

*[Українською](README.md)*

A toolkit for planning road trips together with an agent, on top of the
shared [trip-map](https://trip-map.web.app) map and its MCP worker. The map
itself measures legs (OSRM), keeps the schedule, versions the route, and
stores marks, ratings, and participant discussion — this repository answers
a different question: **where does what gets uploaded to it come from.**

## Idea

The most expensive thing in planning isn't the route — it's **knowledge
about places**: verified coordinates, POI with descriptions and photos,
where to park, where to stay. A route is cheap to rebuild — reorder the
days, shift a departure; knowledge about a place is not cheap to
reconstruct, it's accumulated over time. So here it lives **separately**
from trips: one place card in the library, any number of routes referencing
it. On one trip a place is a pass-through stop, on the next it becomes a
full overnight — and the card stays the same, no rewriting from scratch.

## Two trees

| | Where | What |
|:--|:--|:--|
| **toolkit** | this repository, public | rules, scripts, schemas, skills — the method |
| **data** | a separate directory, private (typically `../travel-data`) | the place library, trip profiles, map bindings, caches, the token |

Data never ends up here — no place name, no `map_id`, no trip date
(`scripts/check_no_data.py` checks this mechanically, see the gate below).
This split means the repository can be cloned and immediately connected to
**your own** map — it contains no file belonging to someone else's trip.

## Quick start

```bash
git clone <this-repo>
cp .travel-local.json.example .travel-local.json   # where YOUR data will live
```

Edit `data_root` in `.travel-local.json` (default — a sibling directory
`../travel-data`; path resolution order: `--data` → `$TRAVEL_DATA` → this
file → `../travel-data`, `scripts/trip_ctx.py`). The worker token goes into
`.env` **in the data root**, not here (see `.env.example` for the variable
names); it's issued on the map: ☰ → "🤖 Connect an AI agent…".

Next — `trip.py new` to start a trip from scratch (the guide walks through
dates, start/end, loop or one-way, interests), or `trip.py import <map_id>`
to pick up a map the group has already filled in and assemble a local
profile and library from it.

Don't have a map or data yet, but want to look around right now —
`examples/demo-trip/` is a small, made-up data root (no token, no
`map_id`) that the generators and scripts already work on:

```bash
python3 scripts/generate_state.py --data examples/demo-trip
python3 scripts/generate_docs.py  --data examples/demo-trip
python3 scripts/check_limits.py   --data examples/demo-trip
```

Details — `examples/demo-trip/README.md`.

## What the skills do

Skills (`.claude/skills/`) are the toolkit's main content. Each explains
**why**, not just what to do: the worker's guide (`trip-map://guide`) sets
the rules, a skill is when and why to apply them in a given situation.

| Skill | Why |
|:--|:--|
| `/trip` | show/switch/create a trip profile — and why mixing up profiles means overwriting someone else's route with your own |
| `/place` | bring a card to the depth its role needs (sketch → basic → full) without rewriting it from scratch — and why a full card for ten candidates, of which two get picked, is waste |
| `/plan-day` | recompute a day's schedule from measured (not eyeballed) legs — and why a straight line on the map is systematically more optimistic than the real road |
| `/publish` | push a route to the shared map through a cycle of gates — and why one `set_route` with the whole route silently kills other people's ratings and threads on points that dropped out of the file |
| `/audit` | assemble a list of candidates worth checking (coordinates, photos, sub-map spread, worker limits, completeness for role) — honestly, as a list, not a "right/wrong" verdict |
| `/alt` | set up a candidate directly on the map instead of holding it in chat — and safely move a point between the route/alternatives layers without losing ratings or threads |
| `/handoff` | file a task for the worker once a local solution has proven itself and is useful beyond just us — and why the handoff boundary sits exactly there (two file types, never code) |

## What the toolkit doesn't do

- **Doesn't duplicate the worker's guide.** `trip-map://guide` is the
  primary source for the map's working cycle; skills rely on it and link to
  it rather than paraphrasing (it changes faster than a duplicate could
  keep up with anyway).
- **Doesn't keep `trip_rules` in itself.** A specific trip's rules live on
  the map; the owner changes them without our involvement. The profile
  (`trip.json`) only holds `poi_categories` — a table of "which MD section
  gets which icon," not a copy of the rules themselves.
- **Doesn't touch user data.** Marks, ratings, discussion threads,
  participants' overnight/parking spots (`get_places`) — we mirror
  (`live_mirror`), never author. Creating and deleting alternatives happens
  only on explicit request, never silently.

## Publicity gate

```bash
python3 scripts/check_no_data.py
```

Scans the files that would actually go into git and fails if a `map_id`, a
library place name, a POI category, or a specific trip's date from the data
root has crept in — along with secrets and the user's absolute paths.
`examples/` is deliberately excluded from this gate: everything in it is
made up for demonstration.

## Origin

This repository came about as a move away from two predecessors, where data
and code were mixed in one tree. You don't need to know that to work with
the toolkit today — the history of decisions and the course of the move are
described separately, in `MIGRATION_PLAN.md` and `STRUCTURE_PROPOSAL.md`, as
a record of how and why the current structure was reached, not as
instructions to act on.

## License

[MIT](LICENSE).
