-- Nothing here should be reported. Expected: 0 violations.
-- Each statement is a case a naive text-matching rule gets wrong.

-- Writes a non-PII column, reads a PII column in the predicate only.
UPDATE dbo.Customer SET Email = 'x@y.com' WHERE SSN = '000-00-0000';

-- Inserts only non-PII columns on a table that holds PII.
INSERT INTO dbo.Customer (Email) VALUES ('a');

-- A PII table read in a subquery. The write target is not PII.
UPDATE dbo.OrderHeader SET Total = 1 WHERE OrderID IN (SELECT id FROM dbo.Employee);

-- A PII table joined for reading only. The write target is not PII.
UPDATE o SET o.Total = 1 FROM dbo.OrderHeader o JOIN dbo.Employee e ON o.eid = e.id;

-- T-SQL two-FROM delete where the PII table is only joined, not deleted from.
DELETE FROM dbo.OrderHeader FROM dbo.OrderHeader o JOIN erp.Contact ct ON o.id = ct.id;

-- MERGE where the PII table is the USING source, not the target.
MERGE dbo.OrderHeader AS t USING dbo.Customer AS s ON t.id = s.id
  WHEN MATCHED THEN UPDATE SET t.Total = 1;

-- Reading PII without writing or copying it.
SELECT SSN FROM dbo.Customer;

-- Non-PII table throughout.
UPDATE dbo.OrderHeader SET Total = 1 WHERE OrderID = 1;
ALTER TABLE dbo.OrderHeader ADD Notes varchar(100);
EXEC sp_rename 'dbo.OrderHeader.Notes', 'Comments', 'COLUMN';

-- ALTER on a non-PII table that only references a PII table in a foreign key.
ALTER TABLE dbo.OrderHeader ADD CONSTRAINT FK_OH_Cust FOREIGN KEY (CustomerID)
  REFERENCES dbo.Customer (CustomerID);

-- The words "then delete" appear in a string, but no row is deleted and the
-- column written is not PII.
MERGE dbo.Customer AS t USING dbo.Stage AS s ON t.id = s.id
  WHEN MATCHED THEN UPDATE SET t.Email = 'then delete';

-- Copy between two tagged tables. The data stays inside the classification.
INSERT INTO dbo.Customer (Email) SELECT EmailAddress FROM erp.Contact;
