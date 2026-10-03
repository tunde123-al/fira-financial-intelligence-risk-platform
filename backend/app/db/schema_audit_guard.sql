-- Append-only guard for audit_log (applied by Alembic revision 0002).
--
-- Rejects UPDATE, DELETE and TRUNCATE on audit_log for every role, including the table owner,
-- so the application path (INSERT + SELECT only) cannot alter history.
--
-- This is database-enforced append-only, NOT cryptographic immutability: a superuser or the table
-- owner can still DROP or disable these triggers, and rows carry no hash chain or signature.
-- Retention purges therefore need a deliberate DBA action (see docs/SECURITY.md).

CREATE OR REPLACE FUNCTION audit_log_reject_mutation() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION USING
        ERRCODE = 'insufficient_privilege',
        MESSAGE = 'audit_log is append-only: ' || TG_OP || ' is not permitted';
END;
$$;

DROP TRIGGER IF EXISTS audit_log_no_update_delete ON audit_log;
CREATE TRIGGER audit_log_no_update_delete
    BEFORE UPDATE OR DELETE ON audit_log
    FOR EACH ROW EXECUTE FUNCTION audit_log_reject_mutation();

DROP TRIGGER IF EXISTS audit_log_no_truncate ON audit_log;
CREATE TRIGGER audit_log_no_truncate
    BEFORE TRUNCATE ON audit_log
    FOR EACH STATEMENT EXECUTE FUNCTION audit_log_reject_mutation();
