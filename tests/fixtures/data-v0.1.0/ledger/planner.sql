BEGIN TRANSACTION;
CREATE TABLE conversions (
    id INTEGER PRIMARY KEY,
    date TEXT NOT NULL,
    amount_cents INTEGER NOT NULL,
    taxable_cents INTEGER NOT NULL,
    source_account TEXT NOT NULL,
    accessible_date TEXT NOT NULL,
    recorded_at TEXT NOT NULL
);
INSERT INTO "conversions" VALUES(1,'2025-11-03',500000,500000,'IRA-1','2030-01-01','2026-10-05T17:51:49+00:00');
CREATE TABLE documents (
    id INTEGER PRIMARY KEY,
    fingerprint TEXT NOT NULL UNIQUE,
    file_name TEXT NOT NULL,
    kind TEXT NOT NULL,
    pages INTEGER NOT NULL DEFAULT 0,
    imported_at TEXT NOT NULL,
    batch TEXT NOT NULL,
    archived_as TEXT
);
INSERT INTO "documents" VALUES(1,'6b51cdb78983a5edd3ba6699147ed1de506d76c4902a679f6fc78abcda73c27e','bank.csv','csv',1,'2026-10-05T17:51:48+00:00','20261005T175147Z','archive/2025/bank.csv');
INSERT INTO "documents" VALUES(2,'aa9907acf2a7be2908637aa7474c27d62ecd80dbad3aef10d94b0222059e9312','div.pdf','pdf',1,'2026-10-05T17:51:48+00:00','20261005T175147Z','archive/2025/div.pdf');
INSERT INTO "documents" VALUES(3,'4f8cb8439a39edf8321b5c4eb5b4b3812968efb643825eb18c7e9a1817e02879','int.pdf','pdf',1,'2026-10-05T17:51:48+00:00','20261005T175147Z','archive/2025/int.pdf');
INSERT INTO "documents" VALUES(4,'f75676559ff577e9776d76d6c72a8564c4fe80fcc7ba354357e7146ce60f6d4f','ofxdownload.csv','csv',1,'2026-10-05T17:51:48+00:00','20261005T175147Z','archive/2025/ofxdownload.csv');
INSERT INTO "documents" VALUES(5,'derived:2025:20261005T175148.723795Z','derived 2025','derived',0,'2026-10-05T17:51:48+00:00','20261005T175147Z',NULL);
INSERT INTO "documents" VALUES(6,'schedule-d-2025:20261005T175148.796186Z','Schedule D 2025','derived',0,'2026-10-05T17:51:48+00:00','20261005T175148.796186Z',NULL);
INSERT INTO "documents" VALUES(7,'derived:2025:20261005T175149.221896Z','derived 2025','derived',0,'2026-10-05T17:51:49+00:00','20261005T175149.221896Z',NULL);
CREATE TABLE facts (
    id INTEGER PRIMARY KEY,
    document_id INTEGER NOT NULL REFERENCES documents(id),
    form TEXT NOT NULL,
    tax_year INTEGER NOT NULL,
    issuer TEXT NOT NULL,
    box TEXT NOT NULL,
    label TEXT NOT NULL,
    value_cents INTEGER NOT NULL,
    page INTEGER NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('accepted', 'pending', 'superseded')),
    value_text TEXT,
    UNIQUE (document_id, form, tax_year, issuer, box)
);
INSERT INTO "facts" VALUES(1,2,'1099-DIV',2025,'Example Brokerage (synthetic)','12','Exempt-interest dividends',0,1,'accepted',NULL);
INSERT INTO "facts" VALUES(2,2,'1099-DIV',2025,'Example Brokerage (synthetic)','1a','Total ordinary dividends',980000,1,'accepted',NULL);
INSERT INTO "facts" VALUES(3,2,'1099-DIV',2025,'Example Brokerage (synthetic)','1b','Qualified dividends',810000,1,'accepted',NULL);
INSERT INTO "facts" VALUES(4,2,'1099-DIV',2025,'Example Brokerage (synthetic)','2a','Total capital gain distributions',15000,1,'accepted',NULL);
INSERT INTO "facts" VALUES(5,3,'1099-INT',2025,'Example Bank (synthetic)','1','Interest income',123456,1,'accepted',NULL);
INSERT INTO "facts" VALUES(6,3,'1099-INT',2025,'Example Bank (synthetic)','4','Federal income tax withheld',0,1,'accepted',NULL);
INSERT INTO "facts" VALUES(7,5,'YTD',2025,'bank','deposits','Bank deposits (YTD)',250000,0,'superseded',NULL);
INSERT INTO "facts" VALUES(8,5,'YTD',2025,'bank','withdrawals','Bank withdrawals (YTD)',4510,0,'superseded',NULL);
INSERT INTO "facts" VALUES(9,5,'YTD',2025,'vanguard_transactions','dividends','Dividends (YTD)',41233,0,'superseded',NULL);
INSERT INTO "facts" VALUES(10,6,'SCH-D',2025,'planner','7','Net short-term gain or loss',0,0,'accepted',NULL);
INSERT INTO "facts" VALUES(11,6,'SCH-D',2025,'planner','13','Capital gain distributions',15000,0,'accepted',NULL);
INSERT INTO "facts" VALUES(12,6,'SCH-D',2025,'planner','15','Net long-term gain or loss',15000,0,'accepted',NULL);
INSERT INTO "facts" VALUES(13,6,'SCH-D',2025,'planner','16','Combined gain or loss',15000,0,'accepted',NULL);
INSERT INTO "facts" VALUES(14,7,'YTD',2025,'bank','deposits','Bank deposits (YTD)',250000,0,'accepted',NULL);
INSERT INTO "facts" VALUES(15,7,'YTD',2025,'bank','withdrawals','Bank withdrawals (YTD)',4510,0,'accepted',NULL);
INSERT INTO "facts" VALUES(16,7,'YTD',2025,'vanguard_transactions','dividends','Dividends (YTD)',41233,0,'accepted',NULL);
CREATE TABLE peak (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    total_cents INTEGER NOT NULL,
    as_of TEXT NOT NULL
);
INSERT INTO "peak" VALUES(1,22051482,'2026-10-05');
CREATE TABLE rows (
    id INTEGER PRIMARY KEY,
    document_id INTEGER NOT NULL REFERENCES documents(id),
    source TEXT NOT NULL,
    kind TEXT NOT NULL,
    row_key TEXT NOT NULL UNIQUE,
    account TEXT NOT NULL,
    date TEXT,
    tax_year INTEGER,
    type TEXT NOT NULL,
    description TEXT NOT NULL,
    symbol TEXT NOT NULL,
    quantity REAL,
    price_cents INTEGER,
    amount_cents INTEGER,
    basis_cents INTEGER,
    acquired TEXT,
    term TEXT,
    line INTEGER NOT NULL,
    raw TEXT NOT NULL
);
INSERT INTO "rows" VALUES(1,1,'bank','bank','bank:T-1001','Checking','2025-01-05',2025,'','CLIENT PAYMENT ACME','',NULL,NULL,250000,NULL,NULL,NULL,2,'T-1001,01/05/2025,CLIENT PAYMENT ACME,2,500.00,Checking');
INSERT INTO "rows" VALUES(2,1,'bank','bank','bank:T-1002','Checking','2025-01-06',2025,'','CARD PURCHASE','',NULL,NULL,-4510,NULL,NULL,NULL,3,'T-1002,01/06/2025,CARD PURCHASE,(45.10),Checking');
INSERT INTO "rows" VALUES(3,4,'vanguard_holdings','holding','vanguard_holdings:2d1b709733cd9e2d8fa4a018','12345678','2026-10-05',2026,'','VANGUARD TOTAL STOCK MARKET INDEX ADMIRAL','VTSAX',1000.123,12050,12051482,NULL,NULL,NULL,2,'12345678,VANGUARD TOTAL STOCK MARKET INDEX ADMIRAL,VTSAX,1000.123,120.50,120,514.82,');
INSERT INTO "rows" VALUES(4,4,'vanguard_holdings','holding','vanguard_holdings:886935240590ab7b32c4dfe3','12345678','2026-10-05',2026,'','VANGUARD FEDERAL MONEY MARKET FUND','VMFXX',5000.0,100,500000,NULL,NULL,NULL,3,'12345678,VANGUARD FEDERAL MONEY MARKET FUND,VMFXX,5000.00,1.00,5000.00,');
INSERT INTO "rows" VALUES(5,4,'vanguard_transactions','transaction','vanguard_transactions:155de1fb996cd5d71606666f','12345678','2025-03-14',2025,'Dividend','Dividend Received','VTSAX',0.0,0,41233,NULL,NULL,NULL,6,'12345678,03/14/2025,03/14/2025,Dividend,Dividend Received,VANGUARD TOTAL STOCK MARKET INDEX ADMIRAL,VTSAX,0.0,0.0,412.33,0.0,412.33,0.0,BROKERAGE,');
INSERT INTO "rows" VALUES(6,4,'vanguard_transactions','transaction','vanguard_transactions:053eade90d65825542c40a7b','12345678','2025-03-14',2025,'Reinvestment','Dividend Reinvestment','VTSAX',3.421,12053,-41233,NULL,NULL,NULL,7,'12345678,03/14/2025,03/14/2025,Reinvestment,Dividend Reinvestment,VANGUARD TOTAL STOCK MARKET INDEX ADMIRAL,VTSAX,3.421,120.53,-412.33,0.0,-412.33,0.0,BROKERAGE,');
INSERT INTO "rows" VALUES(7,4,'vanguard_transactions','transaction','vanguard_transactions:c207fcdbd4b371e3b5f69d03','12345678','2025-06-02',2025,'Sell','Sell','VTSAX',-100.0,12500,1250000,NULL,NULL,NULL,8,'12345678,06/02/2025,06/03/2025,Sell,Sell,VANGUARD TOTAL STOCK MARKET INDEX ADMIRAL,VTSAX,-100.0,125.00,12500.00,0.0,12500.00,0.0,BROKERAGE,');
INSERT INTO "rows" VALUES(8,4,'vanguard_transactions','transaction','vanguard_transactions:dda418912854a75547dc8069','12345678','2025-06-02',2025,'Sell','Sell','VTSAX',-100.0,12500,1250000,NULL,NULL,NULL,9,'12345678,06/02/2025,06/03/2025,Sell,Sell,VANGUARD TOTAL STOCK MARKET INDEX ADMIRAL,VTSAX,-100.0,125.00,12500.00,0.0,12500.00,0.0,BROKERAGE,');
CREATE TABLE schema_version (version INTEGER NOT NULL);
INSERT INTO "schema_version" VALUES(4);
CREATE INDEX facts_by_year ON facts (tax_year, form, status);
CREATE INDEX rows_by_year ON rows (tax_year, source, kind);
COMMIT;
