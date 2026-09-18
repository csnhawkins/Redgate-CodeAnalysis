-- Every statement here should be reported. Expected: 20 violations.
-- Run: flyway check -code   (or: sqlfluff lint for a standalone check)
UPDATE dbo.Customer SET SSN = '000-00-0000' WHERE CustomerID = 1;
UPDATE [dbo].[Customer] SET [TaxID] = '1' WHERE CustomerID = 2;
UPDATE c SET c.SSN = '1' FROM dbo.Customer AS c;
UPDATE c SET c.SSN = '1' FROM dbo.Customer c;
INSERT INTO dbo.Customer (SSN, Email) VALUES ('1','a');
INSERT INTO dbo.Customer VALUES ('1','a');
DELETE FROM dbo.Customer WHERE CustomerID = 2;
MERGE dbo.Customer AS t USING dbo.Stage AS s ON t.id = s.id
  WHEN MATCHED THEN UPDATE SET t.SSN = s.SSN;
MERGE dbo.Customer AS t USING dbo.Stage AS s ON t.id = s.id
  WHEN NOT MATCHED THEN INSERT (SSN, Email) VALUES (s.SSN, s.Email);
MERGE dbo.Customer AS t USING dbo.Stage AS s ON t.id = s.id
  WHEN MATCHED THEN DELETE;
SELECT * INTO dbo.CustomerCopy FROM dbo.Customer;
DELETE FROM dbo.Employee WHERE EmployeeID = 9;
TRUNCATE TABLE dbo.Customer;
DROP TABLE erp.Contact;
ALTER TABLE dbo.Customer ALTER COLUMN SSN varchar(32);
EXEC sp_rename 'dbo.Customer.SSN', 'TaxNumber', 'COLUMN';
EXEC sp_rename 'Customer.SSN', 'TaxNumber', 'COLUMN';
EXEC sp_rename 'MyDb.dbo.Customer', 'Client';
-- Copies PII into a table the classification does not cover.
INSERT INTO dbo.CustomerExport (SSN, Email) SELECT SSN, Email FROM dbo.Customer;
-- sp_rename called with named arguments rather than positionally.
EXEC sp_rename @newname = 'Client', @objname = 'dbo.Customer';
