CREATE TABLE saved_child(id INTEGER PRIMARY KEY,parent_id INTEGER);
INSERT INTO saved_child SELECT id,parent_id FROM child WHERE id=10;
DROP TABLE child;
CREATE TABLE child(id INTEGER PRIMARY KEY,parent_id INTEGER REFERENCES parent(id) DEFERRABLE INITIALLY DEFERRED);
INSERT INTO child SELECT id,parent_id FROM saved_child;
DROP TABLE saved_child;
