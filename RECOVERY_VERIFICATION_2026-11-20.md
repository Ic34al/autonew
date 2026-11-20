# Recovery verification — 2026-11-20

Repository: `Ic34al/autonew`
Recovery branch: `recovery-checkpoint-2026-11-20`
Checkpoint base: `456aa23063b4382a0ae5f363ab65549bd3d143fc`

## Snapshot comparison

The following files match their identified November 16 recovery commits byte-for-byte (same Git blob SHA):
- `bot/modules/uploadchannel.py` — `4fc9398a1a550108a2f1e01eeed2e6bfb4d86cfc`
- `bot/core/ffencoder.py` — `991391d1f3c1b5af6f9e56372853289e447499c0`
- `bot/modules/cmds.py` — `f0e85adf6c314c611eea3c7aafb4e8ccaf5a777e`
- `bot/core/database.py` — `b29f2e02eb9ae3d07976b574b82512db93556078`
- `bot/__init__.py` — `99b5b1ad22329886174df5f8d8f7696d82ed2698`

## Deliberate difference retained

`bot/core/auto_animes.py` is not byte-identical to the November 16 snapshot. The current version has a stricter destination-channel failure guard: it stops if an archive destination is inaccessible rather than silently falling back to the Main Channel. This behavior is retained to avoid unintended channel routing.

## Scope

This recovery audit confirms the files listed above against the referenced snapshots. It is not a claim that every file in the repository was individually compared or that runtime tests/deployment were performed. Automated copyrighted-content acquisition/redistribution code was not reconstructed.
