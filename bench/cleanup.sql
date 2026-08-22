-- Remove everything a benchmark run created.
--
-- A load test that leaves ~380 bytes per span behind will quietly consume a
-- developer's disk one run at a time. Spans carry no foreign key to
-- control.projects (ADR 0003, deliberately), so the telemetry rows have to be
-- deleted explicitly rather than cascading.
DELETE FROM telemetry.spans
 WHERE project_id IN (SELECT id FROM control.projects WHERE slug LIKE 'bench-%');

DELETE FROM telemetry.traces
 WHERE project_id IN (SELECT id FROM control.projects WHERE slug LIKE 'bench-%');

DELETE FROM control.projects WHERE slug LIKE 'bench-%';
