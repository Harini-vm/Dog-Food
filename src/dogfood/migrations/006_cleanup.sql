-- 006: remove the `prizes` table created in 001. Nothing ever used it: places and track winners come
-- from the published results, and certificates record them. An unused table is schema nobody can defend.
DROP TABLE prizes;
