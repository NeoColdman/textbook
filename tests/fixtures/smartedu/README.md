# SmartEdu metadata fixtures

`live_sample_tags.json` and `live_sample_detail.json` are minimal selections of
public platform metadata checked on 2026-10-22. They contain taxonomy, document
identity, file metadata and storage addresses needed to verify the adapter.
Creator/account fields and unrelated descriptive fields have been removed; no
credentials, signed links, PDF bytes or full catalog shard is included.

The fixture deliberately preserves `hierarchies: null` leaves, the real
`version_id`/`update_time` fields and a multipart-style `ti_md5` (`hex-N`) which
must not be treated as a plain MD5. Tests never contact these addresses.
