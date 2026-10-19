# Lab 03: small scientific inputs and an explainable archive

Version 1.0 · 15 October 2026 · Automated Workflows in Satellite Image Processing

Open `Lab03_AOI_Bands_v1_20261015.ipynb`. Write every new table yourself in
`student_schema.sql`, using the interface requirements in `SCHEMA_CONTRACT.md`.
The scientific reader and application code are supplied.

Core work is approximately two hours; the second failure case can be completed
outside class. Run one command at a time. No cluster, GPU or always-on workstation
is required.

## Install the additional packages

Use your existing environment. Conda is recommended for the geospatial packages:

```bash
conda install -c conda-forge "rasterio>=1.4,<2" numpy matplotlib jupyterlab ipykernel
```

Alternatively, if using pip in your existing environment:

```bash
python -m pip install -r requirements.txt
```

`environment.yml` is provided for an optional fresh environment. Do not install both
ways unnecessarily. The service uses Rasterio's custom opener, which requires
Rasterio 1.4 or later. No Earthdata account is required for these public COG assets.

## First steps

All commands assume your terminal is in the extracted project folder containing
`lab03_cli.py`. The student edition deliberately has no new table definitions yet.

```bash
python -m jupyterlab Lab03_AOI_Bands_v1_20261015.ipynb
```

After writing student_schema.sql:

```bash
python check_lab03.py
```

```bash
python lab03_cli.py init --config config/krakow_aoi.json
```

```bash
python lab03_cli.py plan --config config/krakow_aoi.json
```

```bash
python lab03_cli.py acquire --config config/krakow_aoi.json
```

```bash
python lab03_cli.py acquire --config config/krakow_aoi.json
```

```bash
python lab03_cli.py report --config config/krakow_aoi.json
```

The repeat should verify and reuse the existing two subsets, with no COG transfer.
For the default historical scene it also needs no metadata request. A different scene
may require a small live item-metadata request to recover projection information
omitted from the Lab 02 scene table.

## Continue from your own Lab 02 database

Before the first init, optionally copy your Lab 02 database using SQLite backup:

```bash
python lab03_cli.py init --config config/krakow_aoi.json --from-lab2 "C:/path/to/Lab02/state/catalogue.sqlite"
```

Replace that example path with your actual file. The source is opened read-only;
SQLite backup handles its committed state. The destination must not already exist.
After the first copy, omit --from-lab2. This copies the database, not its preview files; their recorded paths still refer to the original locations. Write and check your new schema first.

The default configuration pins a captured June 2025 scene for a reproducible exercise.
If that scene is absent from your copied database, set item_id to a real scene in your
catalogue and choose an AOI fully inside its tile. Inspect its red/nir asset metadata.
You may extend discovery using the supplied Lab 02 logic:

```bash
python lab03_cli.py discover --config config/krakow_aoi.json
```

Discovery does not silently change the pinned item_id. The initial start remains
21 September 2026; adjust it before creating a stream for a future course date.
If no database is copied, init seeds actual captured catalogue records from Lab 01.
It does not pretend these records were just returned by a live discovery request.

## What is downloaded and stored?

Exactly the B04/red and B08/nir assets of one selected Sentinel-2 L2A tile are used.
Native 10 m pixels are read with bounded HTTPS ranges. Servers returning a full 200
response instead of a 206 range response are rejected without reading that body.
The code does not download full SAFE products or full bands and then crop them.

The AOI is a west/south/east/north rectangle in EPSG:4326. It is transformed to the
native UTM grid, rounded outward to pixel boundaries, and pixels whose centers fall
outside the AOI are set to nodata. A one-pixel border may lie outside the geographic
rectangle, but no valid data is retained there. Red and nir must have identical
native grids; this lab does not silently reproject or resample them. The AOI must fit
within one tile; mosaicking is a later topic.

Defaults: 500,000 pixels per band; 12 MiB of HTTP body data per source band; 8 MiB
per output file; 150 MiB data-directory budget per workspace. The raw pixel estimate
excludes TIFF metadata and compression. The network cap is separate: COG blocks and
headers may be much larger than the output crop. A true read error or cap failure
remains a failed attempt and can be retried with the same configuration.

Native uint16 values, nodata, reflectance scale and offset are preserved. The
notebook explicitly applies scale and offset before its optional NDVI inspection.
B8A/nir08 is not a replacement for this 10 m B08 exercise. No scene classification or
cloud mask is downloaded, so the demonstration NDVI is not a cloud-screened product.

## Storage layout

The program creates data/<item_id>/<aoi_key-prefix>/<band>-<artifact-id-prefix>.tif.
Explain why both scene and AOI appear in the path. The database stores full IDs,
not just the shortened filename components. It is the authority for lineage; a
filename alone does not preserve parameters, calibration or attempt history.

## Isolated failure cases

Generate each case once. These commands intentionally end with a failure exit code 1.
They create independent databases/data under cases/ and leave the main archive alone.
A repeated case command refuses to overwrite the existing case; use its configuration
to inspect or recover it. All named configuration files below are generated by the
case command before the failure; none are deliberately missing files.

Network interruption, with one band complete and the next failing:

```bash
python lab03_cli.py case --config config/krakow_aoi.json --name network
```

Investigate before recovering:

```bash
python lab03_cli.py report --config cases/network/config.json
```

```bash
python lab03_cli.py acquire --config cases/network/config.json
```

Recorded success followed by a missing red file:

```bash
python lab03_cli.py case --config config/krakow_aoi.json --name missing
```

```bash
python lab03_cli.py report --config cases/missing/config.json
```

```bash
python lab03_cli.py acquire --config cases/missing/config.json
```

Optional: --name corrupt truncates an actual case file; --name interrupted stops
between writing a valid temporary subset and finalizing/registering it. Both use
real imagery. These operations only alter their own generated case folders.

## Relationship to the target course schema

Lab 03 retains the existing five tables and adds the five student-defined tables.
Existing preview rows in downloads are retained. We do not store an AOI crop as if
it were the complete remote asset: subset_requests gives each AOI its own identity.
The earlier full-course sketch assumed source asset downloads; this is an explicit
refinement for cropped inputs. In the later general processing framework,
subset_requests maps to tasks, its recipe/AOI to parameters, attempts to executions,
and artifacts to versioned outputs/products. No full 25-table migration is required
for this lab. Two supplied columns, command and config_json, are added to runs so
acquisition/verification invocations and their resolved paths can be distinguished.

## Data and evidence

- fixtures/real_catalogue.json contains actual provider responses reused from Lab 01.
- fixtures/real_red_aoi.tif and real_nir_aoi.tif are actual small subsets obtained
  with the bounded reader for offline checks. They are not substituted during a
  live acquire command. Their source and hashes are in aoi_manifest.json.
- The student notebook, schema contract and check script contain no reference DDL
  for the new tables. Instructor answers are distributed separately.
- Partial COG range reads are never marked as complete raw-band downloads. A local
  subset hash describes its output GeoTIFF, not the entire remote COG.
- Do not commit state/, data/ or cases/ to Git. Submit your SQL, notebook explanations,
  diagnostic queries and a small report instead.

Reference docs: https://rasterio.readthedocs.io/en/latest/topics/windowed-rw.html
and https://github.com/Element84/earth-search/blob/main/README.md
