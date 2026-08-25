# Product

## Register

product

## Users

The primary user is a vehicle owner reviewing their own car data on a desktop. The main job is historical investigation: understand where the car went, how it was driven, how the vehicle behaved, and what changed before or during a problem. A live view remains important for setup, diagnosis, and an occasional at-a-glance cockpit.

## Product Purpose

Freematics Vehicle Telemetry turns the broadest safely queryable set of OBD-II, GNSS, motion, connectivity, and device readings into a durable vehicle record. It groups telemetry into trips, preserves the raw metric timeline, surfaces diagnostic trouble codes and vehicle identity, and makes both trip-level summaries and detailed investigation available in Grafana.

Success means the user can answer three questions without hunting: is the device and vehicle healthy now, what happened on a specific trip, and what does the complete evidence show over time?

## Brand Personality

Precise, capable, composed. The interface should feel like a well-engineered instrument: information-dense without being oppressive, visually distinctive without imitating a sports-car dashboard, and direct about missing or unsupported data.

## Anti-references

- No generic neon automotive cockpit, carbon-fibre texture, fake analogue dials, or redline theatrics.
- No wall of identical stat cards or gauges.
- No decorative gradients, glass panels, or colour used without meaning.
- No hiding raw data or ambiguity behind a simplified health score.

## Design Principles

1. History first, live when useful: lead with trips and time, then provide a compact live status layer.
2. Overview to evidence: summaries must drill down to the exact underlying metric series and route.
3. Density with hierarchy: show a lot, but group it by the questions a vehicle owner actually asks.
4. Absence is data: distinguish unsupported, unavailable, stale, offline, and genuinely zero values.
5. Preserve the record: raw ECU metrics remain accessible even when a curated view is more readable.

## Accessibility & Inclusion

Use WCAG AA contrast, do not rely on colour alone for state, use colour-blind-safe series choices, preserve legible labels at dashboard density, and avoid decorative motion. Panels must remain understandable with Grafana's light or dark theme and at narrower desktop or tablet widths.
