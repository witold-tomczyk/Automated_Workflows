# Lab 02: Sentinel discovery from the command line

Version 1.0 · 5 October 2026 · Python 3.10+

Open Lab02_Sentinel_CLI_v1_20261005.ipynb for the exercises. Edit student_tasks.py.
Everything else is supplied. Three deliberate defects remain in the student edition;
repair them before starting the weekly live experiment.

The runtime and checks use only the Python standard library. Jupyter is needed only
for the notebook interface. Use your existing environment.

Commands below assume your terminal is in the folder containing sentinel_cli.py:

```bash
python sentinel_cli.py --help
```

```bash
python check_lab02.py
```

```bash
python sentinel_cli.py discover --config config/krakow_week.json --preview
```

```bash
python sentinel_cli.py report --config config/krakow_week.json
```

## Files

- student_tasks.py: the three repair functions.
- sentinel_cli.py: the supplied command parser and entry point.
- workflow.py: supplied orchestration, query membership and run journal.
- lab01_service.py: repaired functions extracted from Lab 01 instructor v3.
- config/krakow_week.json: live weekly experiment; initial start 21 September 2026.
- config/historical_check.json: optional real live search over the Lab 01 date window.
- fixtures/: captured actual provider records and JPEG, for repeatable checks only.
- check_lab02.py: exercise checks and service integration checks.

Each invocation has a fixed end. Default end is current UTC. An explicit --until is
for controlled experiments; it must not precede saved progress. Configuration paths
resolve relative to the configuration file. Do not change initial_start during a week.
For a future course date, edit the shipped initial start before the first live run.

At most one JPEG preview is requested per discovery command, and it is optional.
A verified existing preview is reused. The downloader enforces a 5 MiB cap and accepts
only the thumbnail JPEG asset. No bands or complete scene products are downloaded.
Selection prioritizes acquisition time, not cloud cover; unavailable JPEGs are skipped.

Run manually at least four times in seven days, including an immediate rerun and a
48 hour gap. A powered off laptop performs no work. Its next invocation resumes from
saved progress with overlap. Finite overlap is not a guarantee against arbitrarily
late publication; --lookback-days 30 widens reconciliation within the initial interval.

The database retains the original Lab 01 scenes/streams/downloads schema, adding runs
and stream_scenes. An existing compatible Lab 01 database may be selected; its scene
membership is populated as this service revisits items, and the stream-specific counts
then mean first association by Lab 02 rather than first-ever insertion. For the lab,
use the separate default state directory. Query streams are keyed by endpoint,
collection and bounding box, as in Lab 01. Run one command at a time.

Discovery failures preserve committed pages without advancing the watermark. Preview
failures happen after discovery; they produce a failed command but retain the completed
discovery watermark. An abruptly killed process may leave a run marked running. It is
an interrupted record, not proof that a background worker is alive. Restart the command;
this lab does not implement parallel workers, a scheduler or exactly-once transactions
across an operating-system kill between page and membership commits.

Logs go to stderr and the configured events.log; JSON summaries go to stdout. Exit code
0 means the command completed, including zero new scenes; 1 means operational failure;
2 means argparse rejected the command syntax. The report age measures time since
acquisition of the newest catalogued scene, not provider publication latency.

Historical live check (separate database):

```bash
python sentinel_cli.py discover --config config/historical_check.json --until 2025-06-15T00:00:00Z --preview
```

Actual request availability depends on your network and the provider. The service does
not silently fall back to recorded fixtures. Provider documentation:
https://github.com/Element84/earth-search

The default geographic box covers Kraków. Change it before beginning if you prefer
another region. Avoid expanding the box excessively; reaching the page cap is a failure,
not complete discovery. Keep each student's database local; do not share it via Git.
