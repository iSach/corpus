ALTER TABLE fields ADD COLUMN color TEXT NOT NULL DEFAULT '';
-- Keep each existing topic close to its old look: before this, the shape picked the color.
UPDATE fields SET color = CASE symbol
  WHEN 'triangle' THEN 'blue' WHEN 'circle' THEN 'vermillion' WHEN 'square' THEN 'green'
  WHEN 'diamond' THEN 'purple' WHEN 'star' THEN 'yellow' WHEN 'cross' THEN 'purple'
  WHEN 'hexagon' THEN 'sky' WHEN 'pentagon' THEN 'vermillion' ELSE '' END;
INSERT INTO schema_migrations(version) VALUES(15);
