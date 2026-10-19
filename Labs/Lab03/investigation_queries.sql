-- Write your own diagnostic SQL for the five new tables.
-- One worked example follows. Add your case queries below it.
SELECT r.run_id,r.command,a.item_id,a.asset_key,s.aoi_key,d.status,d.stage,
       d.network_bytes,d.http_status,d.error_code,d.error_message
FROM download_attempts d JOIN runs r ON r.run_id=d.run_id
JOIN subset_requests s ON s.subset_id=d.subset_id
JOIN assets a ON a.asset_id=s.asset_id
ORDER BY d.started_at,d.attempt_id;

-- Case A: show failed band, partial success and the later recovery history.

-- Case B: show successful historical acquisition but current missing/corrupt location.

-- AOI identity: show one source band with separate requests for two AOIs.
