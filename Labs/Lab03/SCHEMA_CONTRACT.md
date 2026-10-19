# Database design contract · Lab 03 v1.0

You write the complete SQL definitions of all five new tables in `student_schema.sql`.
The supplied Python needs the column names below. You choose and write the primary
keys, foreign keys, unique constraints, checks, and indexes. There are no incomplete
CREATE TABLE statements to fill in. The original five Lab 02 tables are supplied.

All IDs are TEXT, non-null, and supplied by Python. All timestamps are UTC TEXT.
All non-null fields below must reject NULL. JSON fields contain valid JSON text.
The supplied code calculates IDs and SHA-256 values, not SQLite.

| Table | One row represents | Primary key |
|---|---|---|
| assets | One observed metadata revision of a scene's red or nir asset | asset_id |
| subset_requests | One asset, AOI and extraction recipe combination | subset_id |
| download_attempts | One attempt to acquire or verify that subset | attempt_id |
| artifacts | One distinct, successfully stored subset byte sequence | artifact_id |
| artifact_locations | One known local path for an artifact | location_id |

## assets

| Column | Type | May be NULL? | Meaning |
|---|---|---|---|
| asset_id | TEXT | No | Source asset revision identity |
| collection | TEXT | No | Parent scene collection |
| item_id | TEXT | No | Parent scene ID |
| asset_key | TEXT | No | Only red or nir in this lab |
| source_url | TEXT | No | Remote COG URL |
| metadata_json | TEXT | No | Provider asset metadata, including scale/offset |
| metadata_sha256 | TEXT | No | Hash of canonical source metadata |
| created_at | TEXT | No | Local registration time |

Reference the composite scene key `(collection,item_id)` in `scenes`. The tuple
`(collection,item_id,asset_key,metadata_sha256)` must be unique. Registering the same
source metadata twice must not create two logical asset revisions.

## subset_requests

| Column | Type | May be NULL? | Meaning |
|---|---|---|---|
| subset_id | TEXT | No | Request identity |
| asset_id | TEXT | No | Parent asset revision |
| aoi_key | TEXT | No | Hash of AOI geometry and pixel-inclusion rule |
| aoi_json | TEXT | No | EPSG:4326 bounding box definition |
| recipe_version | TEXT | No | Extraction/masking recipe identity |
| status | TEXT | No | pending, ready, or failed |
| latest_artifact_id | TEXT | Yes | Current successfully stored result, if any |
| updated_at | TEXT | No | Most recent state update |

Reference `assets`. Make `(asset_id,aoi_key,recipe_version)` unique. Different AOIs
of the same source band must coexist. A ready request must have an artifact. Its
latest artifact must belong to this same subset request, not a different AOI.

## download_attempts

| Column | Type | May be NULL? | Meaning |
|---|---|---|---|
| attempt_id | TEXT | No | Attempt identity |
| subset_id | TEXT | No | Request being attempted |
| run_id | TEXT | No | Invoking Lab 02/03 run |
| status | TEXT | No | running, succeeded, reused, or failed |
| stage | TEXT | No | cache_verify, read_window, write_subset, validate_subset, before_finalize, finalize, register, complete |
| started_at | TEXT | No | Start time |
| finished_at | TEXT | Yes | End time; absent only while running |
| network_bytes | INTEGER | No | Nonnegative HTTP response body bytes actually received; default 0 |
| http_status | INTEGER | Yes | Last remote HTTP status when available; cache checks have none |
| artifact_id | TEXT | Yes | Result or inspected artifact |
| error_code | TEXT | Yes | Structured failure category |
| error_message | TEXT | Yes | Explanation |

Reference the request and `runs.run_id`. Running rows must have no finish timestamp;
terminal rows must have one. Succeeded/reused rows must identify an artifact. An
artifact referenced by an attempt must belong to that same subset request. Preserve
failed rows when a new attempt succeeds. Do not make subset_id or run_id individually
unique: both can legitimately appear in many attempts.

## artifacts

| Column | Type | May be NULL? | Meaning |
|---|---|---|---|
| artifact_id | TEXT | No | Subset byte identity |
| subset_id | TEXT | No | Parent request |
| created_by_attempt_id | TEXT | No | Attempt that first registered these bytes |
| sha256 | TEXT | No | Actual local file SHA-256 |
| size_bytes | INTEGER | No | Positive stored byte count |
| raster_json | TEXT | No | CRS, transform, shape, source window, nodata, scale/offset and recipe |
| created_at | TEXT | No | Registration time |

Reference the request and creating attempt. The creating attempt must belong to the
same request. Make `(subset_id,sha256)` unique. Retrying a transfer can reproduce
identical bytes and refer to this same artifact while retaining its own attempt row.

## artifact_locations

| Column | Type | May be NULL? | Meaning |
|---|---|---|---|
| location_id | TEXT | No | Location identity |
| artifact_id | TEXT | No | Artifact at this location |
| local_path | TEXT | No | Unique path |
| status | TEXT | No | available, missing, or corrupt |
| checked_at | TEXT | No | Latest actual verification time |

Reference the artifact. A database foreign key cannot establish whether the file
exists; a filesystem check must update this state. Locations can change without
changing the artifact's byte identity.

## Composite references and circular relationships

An existing artifact ID alone does not prove it belongs to the right AOI. Enforce
these relationships as pairs:

- subset_requests `(latest_artifact_id,subset_id)` → artifacts `(artifact_id,subset_id)`
- download_attempts `(artifact_id,subset_id)` → artifacts `(artifact_id,subset_id)`
- artifacts `(created_by_attempt_id,subset_id)` → download_attempts `(attempt_id,subset_id)`

SQLite requires each referenced parent pair to have a matching PRIMARY KEY or UNIQUE
constraint, even if one member is already a primary key. The nullable artifact links
allow inserting a request and a running attempt before inserting its result. A NULL
optional reference is different from a nonexistent parent.

Do not add a new status for every failure cause. Status describes lifecycle; stage,
error code and message explain why it failed. Indexes on attempt run_id and subset_id
are a useful extension. Keep schema SQL to table/index definitions; do not drop or
change the supplied original tables.

Python enables foreign key enforcement on every connection. When testing INSERTs in
DB Browser, enable it there too with PRAGMA foreign_keys = ON; it is a per-connection
setting, not merely a property of the table definition.
