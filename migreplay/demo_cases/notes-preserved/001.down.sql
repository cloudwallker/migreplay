ALTER TABLE notes ADD COLUMN body TEXT;
UPDATE notes SET body=(SELECT body FROM notes_backup WHERE notes_backup.id=notes.id);
DROP TABLE notes_backup;
