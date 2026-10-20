-- Weekly historian v2: the whole structured report (headline, highlights, stories, server notes) with version=2.
-- observations stays as the derived v1 view so an unchanged site keeps rendering.
ALTER TABLE historian_runs ADD COLUMN report JSON NULL;
