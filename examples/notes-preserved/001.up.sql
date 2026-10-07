CREATE TABLE notes_backup(id INTEGER PRIMARY KEY,body TEXT);
INSERT INTO notes_backup SELECT id,body FROM notes;
ALTER TABLE notes DROP COLUMN body;
