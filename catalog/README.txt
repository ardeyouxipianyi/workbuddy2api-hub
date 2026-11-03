product-config-<realm>.json is a runtime dump: the last /v3/config response
body the gateway fetched successfully from the upstream API (one file per
realm), kept as an on-disk copy of the most recent catalogue.

One reader exists: the media layer (wb_media / the /v1/media/models route)
falls back to this file on cold start, because the media catalogue lives in
`data.models[].tags` and the chat projection drops those entries. In-memory
caching covers everything after the first successful fetch, so the file is
only touched when the process has not seen a catalogue yet.

Deleting these files is still safe: at worst the next media request triggers
one extra read-only /v3/config fetch, and the next successful fetch rewrites
them.
