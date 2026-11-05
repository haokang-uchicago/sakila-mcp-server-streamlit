-- Assignment 5 · a read-only identity for the question-answering app (run once in Snowsight as ACCOUNTADMIN)
-- Role SAKILA_DW_READER: can read SAKILA.DW and use CLASS_WH, nothing else
-- User SAKILA_QA_APP:    a service account for the MCP server and the Streamlit app; key-pair sign-in, no password
USE ROLE ACCOUNTADMIN;

-- ---------- Role: what the badge opens ----------
CREATE ROLE IF NOT EXISTS SAKILA_DW_READER COMMENT = 'Assignment 5: read-only on SAKILA.DW';
GRANT USAGE  ON WAREHOUSE CLASS_WH              TO ROLE SAKILA_DW_READER;  -- may run queries on this warehouse
GRANT USAGE  ON DATABASE  SAKILA                TO ROLE SAKILA_DW_READER;  -- may enter the SAKILA database
GRANT USAGE  ON SCHEMA    SAKILA.DW             TO ROLE SAKILA_DW_READER;  -- may enter DW only (not OLTP or STAGING)
GRANT SELECT ON ALL TABLES IN SCHEMA SAKILA.DW  TO ROLE SAKILA_DW_READER;  -- may read; cannot insert, update or delete
-- No FUTURE grant on purpose: a schema-level FUTURE grant would override the database-level FUTURE
-- grant Week 4 gave CLAUDE_RO. If DW gets a new table, run the line above again.

-- ---------- User: the app's own sign-in, locked with a public key ----------
CREATE USER IF NOT EXISTS SAKILA_QA_APP
  TYPE = SERVICE                       -- service account: key-pair only, cannot sign in to Snowsight with a password
  DEFAULT_ROLE = SAKILA_DW_READER
  DEFAULT_WAREHOUSE = CLASS_WH
  DEFAULT_NAMESPACE = SAKILA.DW
  COMMENT = 'Assignment 5: the MCP server and Streamlit app sign in as this user';
-- Paste the body of ~/.snowflake/sakila_qa_app.pub (without the BEGIN/END lines). A public key is not a secret.
ALTER USER SAKILA_QA_APP SET RSA_PUBLIC_KEY = '<contents of sakila_qa_app.pub>';
GRANT ROLE SAKILA_DW_READER TO USER SAKILA_QA_APP;

-- ---------- Checks ----------
SHOW GRANTS TO ROLE SAKILA_DW_READER;   -- expect: USAGE on CLASS_WH, SAKILA and SAKILA.DW; SELECT on the 6 DW tables
DESC USER SAKILA_QA_APP;                -- the RSA_PUBLIC_KEY_FP row should show SHA256:... once the key is set

-- ---------- Read-only proof (run as the app user, e.g. `snow sql -c sakila_qa_app -q "..."`) ----------
-- SELECT COUNT(*) FROM SAKILA.DW.FACT_PAYMENT;           -- works: 16044
-- SELECT COUNT(*) FROM SAKILA.OLTP.PAYMENT;              -- fails: no USAGE on schema OLTP
-- DELETE FROM SAKILA.DW.FACT_PAYMENT WHERE 1 = 0;        -- fails: no DELETE privilege
