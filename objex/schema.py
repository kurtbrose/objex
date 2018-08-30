"""
Implements a star-schema on object.

All foreign keys are object ids.
pycode, pytype, pyframe, module, etc associate with an object row
via their "object" column
"""
_SCHEMA = '''
CREATE TABLE meta (
    id INTEGER PRIMARY KEY,
    ts TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    pid INTEGER NOT NULL,
    hostname TEXT NOT NULL,
    memory_mb INTEGER NOT NULL,
    gc_info TEXT NOT NULL,
    duration_s REAL
);

CREATE TABLE object (
    id INTEGER PRIMARY KEY,
    pytype INTEGER NOT NULL,
    size INTEGER NOT NULL,
    len INTEGER,
    refcount INTEGER NOT NULL,
    in_gc_objects INTEGER NOT NULL,
    is_gc_tracked INTEGER NOT NULL
);

CREATE TABLE pytype (
    id INTEGER PRIMARY KEY,
    object INTEGER NOT NULL,
    module INTEGER, -- object-id of module
    name TEXT NOT NULL -- typenames are okay
);

CREATE TABLE pytype_bases (
    id INTEGER PRIMARY KEY,
    obj_id INTEGER NOT NULL, -- object id
    base_obj_id INTEGER NOT NULL -- object id
);

CREATE TABLE module (
    id INTEGER PRIMARY KEY,
    object INTEGER NOT NULL,
    file TEXT NOT NULL,
    name TEXT NOT NULL
);

CREATE TABLE pyframe (
    id INTEGER PRIMARY KEY,
    object INTEGER NOT NULL,
    f_back_obj_id INTEGER, -- parent pointer in stack
    f_code_obj_id INTEGER NOT NULL, -- object (code)
    f_lasti INTEGER NOT NULL, -- last instruction executed in code
    f_lineno INTEGER NOT NULL, -- line number in code
    trace TEXT NOT NULL  -- segment of a traceback kind of format (used for display to user)
);

CREATE TABLE thread (
    id INTEGER PRIMARY KEY,
    stack_obj_id INTEGER NOT NULL, -- pyframe top of stack
    thread_id INTEGER NOT NULL -- os thread id
);

CREATE TABLE pycode (  -- needed to assocaite pyframes back to function objects
    id INTEGER PRIMARY KEY,
    object INTEGER NOT NULL,
    co_name TEXT NOT NULL
);

CREATE TABLE function (
    id INTEGER PRIMARY KEY,
    object INTEGER NOT NULL,
    func_name TEXT NOT NULL,
    func_code_obj_id INTEGER NOT NULL, -- object-id of pycode
    module_obj_id INTEGER -- object-id of module
);

CREATE TABLE reference (
    src INTEGER NOT NULL, -- object
    dst INTEGER NOT NULL, -- object
    ref TEXT NOT NULL -- keys *might* be okay
);

CREATE TABLE gc_referrer (
    src INTEGER NOT NULL,
    dst INTEGER NOT NULL
);

CREATE TABLE gc_referent (
    src INTEGER NOT NULL,
    dst INTEGER NOT NULL
);
'''


# these indices are applied when switching from
# "data-collection" mode to "analysis mode"
_INDICES = '''
CREATE INDEX pytype_object ON pytype(object);
CREATE INDEX pytype_name ON pytype(name);
CREATE INDEX object_pytype ON object(pytype);
CREATE INDEX object_size ON object(size);
CREATE INDEX object_len ON object(len);
CREATE INDEX object_all ON object(pytype, size, len);
CREATE INDEX reference_src ON reference(src);
CREATE INDEX reference_dst ON reference(dst);
CREATE INDEX reference_ref ON reference(ref);
CREATE INDEX reference_all ON reference(src, dst, ref);
CREATE INDEX function_object ON function(object);
CREATE INDEX pyframe_object ON pyframe(object);
CREATE INDEX pycode_object ON pycode(object);
CREATE INDEX module_object ON module(object);
'''
