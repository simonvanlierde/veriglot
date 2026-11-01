# data

Put inputs in `raw/` and derived files in `processed/`; create them as you need them.
`raw/` is read-only input: never edit it in place. The notebooks rebuild `processed/`
from `raw/`, so you can delete it.

Record the provenance of anything in `raw/` here. Add these directories to
`.gitignore` if the inputs are large or private.
