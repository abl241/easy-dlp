# Apple Music-inspired desktop overhaul

Branch: `codex/apple-music-ui-overhaul`.

The app retains CustomTkinter, its job queues, pagination, thumbnail cache, and
media processing pipeline. There are no new dependencies.

## User-facing changes

- Persistent Music, Video, and Settings pages with a sidebar. Downloads and
  Recent open the activity drawer without replacing the current results.
- Always-visible search accepts either text or a single link. Music links route
  through the existing YouTube/Spotify resolver. Bulk links and artist/title
  lists remain available in the import disclosure.
- Music-inspired gray surfaces, icon-derived blue/violet accents, consistent spacing,
  clearer headings, and quieter row actions. Music artwork reserves square
  space; video artwork stays widescreen. Image aspect ratios are preserved.
- Music rows emphasize artist and duration. Full titles and source metadata
  are available in delayed hover tooltips.
- Compact activity summary with an active count and progress for the oldest
  active item. Background jobs do not automatically expand the drawer.
- Interruptible 140 ms progress transitions; Reduce motion in Settings disables
  them. This is an app preference, not automatic detection of OS Reduce Motion.
- Command-F focuses search, Command-comma opens Settings, and Escape closes
  match review, the drawer, or bulk import. Windows/Linux use Control.
- Empty states explain the next action; switching pages retains existing rows
  and the query. Repeated primary search submission is blocked while its current
  lookup is active.

This remains a CustomTkinter app; the materials are solid colors rather than
native macOS vibrancy. No playback functionality is implied by the design.

## Verification

Run native integration tests in a graphical desktop session:

```sh
.venv/bin/python -m unittest discover -s tests -v
```

Tests use temporary settings and mocked job submission, with no media downloads.
Coverage includes URL routing, preservation of hidden track-list drafts, repeat
submission, retained results, bulk import, keyboard focus, placeholder behavior,
empty states, both themes, minimum window sizing, image proportions, reduced
motion, and stable workspace height during progress updates.

For an isolated synthetic rendering comparison:

```sh
.venv/bin/python scripts/benchmark_ui.py /path/to/checkout
```

An initial paired run on the development Mac (Python 3.12, Tk 9, CustomTkinter 6)
measured 100-row rendering at 10,073 ms before and 9,693 ms after; app construction
was 653 ms before and 592 ms after. Maximum 10 ms heartbeat intervals during row
creation were 514 ms before and 462 ms after. These are single-run observations,
not a performance guarantee. They exclude network/media processing and do not
establish a 60 fps frame rate. Large-list rendering remains an area for future
virtualization; this change preserves the existing three-row chunking.

A real music search was also checked in the native preview: results, artwork,
and the persistent query field displayed successfully. Downloads were not run
as part of the visual check.
