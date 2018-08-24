
def _run_ddl(conn, ddl_block):
    """
    break a ; delimited list of DDL statements into
    a list of individual statements and execute them
    in conn
    """
    for ddl_stmt in ddl_block.split(';'):
        ddl_stmt = ddl_stmt.strip()
        if ddl_stmt:
            conn.execute(ddl_stmt)
    return
