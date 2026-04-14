import sys
import sqlite3
import argparse
import subprocess
import utils
import shutil
import tempfile
import os
import sqlglot
from sqlglot import (Expression, expressions)
from dataclasses import dataclass, field, asdict
import json
import conflict_pairs
from collections import defaultdict
from typing import Literal, Iterable
from contextlib import closing

@dataclass
class DiffBuckets:
    matched_ours_indices: list[int]
    extra_ours_indices: list[int]
    extra_theirs_indices: list[int]
    conflict_pairs: dict[tuple[int, int], list[conflict_pairs.ConflictPairs]]

empty_set = set()
empty_list = []
KeyValueData = tuple[int, type, dict[str, Expression] | None]
KeyTuple = tuple[Expression, ...]

@dataclass
class InvalidTableState:
    invalid_reasons: list[dict[str, object]]
    base_to_ours: list[int] = field(default_factory=list)
    base_to_theirs: list[int] = field(default_factory=list)

@dataclass
class UniqueIndexState:
    index_name: str
    column_names: tuple[str, ...]
    values_to_stmt_index: dict[tuple[object, ...], int] = field(default_factory=dict)

# per table
@dataclass
class TableConflictState:
    primary_key_columns_to_index: dict[str, int]
    primary_key_value_to_data: dict[KeyTuple, KeyValueData]
    unique_key_column_names: set[str]
    unique_indexes: list[UniqueIndexState]
    # look at child_references columns from parent_references from TableForeignLink
    unique_indexes_outdated_values_as_parent: dict[tuple[object, ...], int] = field(default_factory=dict)
    # look at parent_reference columns from child_references from TableForeignLink
    column_values_as_child: dict[tuple[object, ...], int] = field(default_factory=dict)

@dataclass
class ForeignLinkMetadata:
    linked_table: str
    linked_table_columns: tuple[str, ...]

@dataclass
class TableForeignLink:
    # OUR TABLE"S parent references: keyed by columns of our table
    parent_references: dict[tuple[str, ...], ForeignLinkMetadata] = field(default_factory=dict)
    # OUR TABLE"S children references: keyed by columns of our table
    child_references: dict[tuple[str, ...], ForeignLinkMetadata] = field(default_factory=dict)

transaction_begin = "BEGIN TRANSACTION;"
transaction_end = "COMMIT;"

def apply_sql_to_temp_db(source_db_path, sql_script):
    fd, temp_db_path = tempfile.mkstemp(suffix=".db")
    os.close(fd) # close low-level file descriptor
    try:
        shutil.copy2(source_db_path, temp_db_path)
        proc = subprocess.run(
            ["sqlite3", temp_db_path],
            input=sql_script,      # SQL string in memory
            text=True,
            capture_output=True
        )
        if proc.returncode != 0:
            raise RuntimeError(f"sqlite3 failed: {proc.stderr.strip()}")

        return temp_db_path  # caller can keep using this merged temp DB
    except Exception:
        if os.path.exists(temp_db_path):
            os.remove(temp_db_path)
        raise

def get_target_table(expr: Expression) -> expressions.Table:
    """
    Return the top-level target table for INSERT / UPDATE / DELETE.
    Avoids expr.find(expressions.Table), which can accidentally match nested
    tables in subqueries.
    """
    table_expr: Expression | None = None
    if isinstance(expr, expressions.Insert):
        target = expr.this
        # INSERT INTO t (a, b) ... parses target as Schema(this=Table(...), expressions=[...])
        table_expr = target.this if isinstance(target, expressions.Schema) else target
    elif isinstance(expr, (expressions.Update, expressions.Delete)):
        table_expr = expr.this
    else:
        raise TypeError(f"Unsupported DML type: {type(expr).__name__}")
    if not isinstance(table_expr, expressions.Table):
        raise ValueError(
            f"Could not resolve target table for {type(expr).__name__}: {expr.sql()}"
        )
    return table_expr

def pragma_integrity_check_table_failed_and_recorded(
        invalid_tables: dict[str, InvalidTableState], 
        table_name: str, 
        branch_and_cursor: Iterable[tuple[Literal['base', 'ours', 'theirs'], sqlite3.Cursor]]):
    # note its possible the table have yet to exist in which case sqlite3 would give operational error
    # though in that case it would be a schema diff problem which we aim to flag error for now
    for branch, cursor in branch_and_cursor:
        integrity_check = cursor.execute(f"PRAGMA integrity_check({table_name});").fetchone()["integrity_check"]
        if integrity_check != "ok":
            invalid_tables[table_name].invalid_reasons.append({
                "failed_branch": branch,
                "failure_type": "pragma_integrity_check_failed",
                "failure_rows_or_details": integrity_check
                })

def check_valid_tables(
        base_to_ours_parsed: list[Expression], 
        base_to_theirs_parsed: list[Expression], 
        base_cursor: sqlite3.Cursor, 
        ours_cursor: sqlite3.Cursor, 
        theirs_cursor: sqlite3.Cursor) -> tuple[set[str], dict[str, InvalidTableState]]:
    '''run pragma foreign key and integrity check and returns invalid tables and tables set'''
    tables_set = set()
    invalid_tables = defaultdict(lambda: InvalidTableState([]))
    branch_and_cursor = tuple(zip(("base", "ours", "theirs"), (base_cursor, ours_cursor, theirs_cursor)))
    # temporary table keyed by tuple[table name, branch, and a boolean s.t. True if child table else True ([parent table)]
    invalid_foreign_key_tables: defaultdict[
        str, defaultdict[tuple[Literal["base", "ours", "theirs"], bool], list[sqlite3.Row]]
    ] = defaultdict(lambda: defaultdict(list)) 
    for branch, cursor in branch_and_cursor:
        # each row returns dictionary with keys "table", "rowid" (if exists), "parent" and "fkid"
        rows = cursor.execute(f"PRAGMA foreign_key_check;").fetchall()
        for row in rows:
            invalid_foreign_key_tables[row["table"]][(branch, True)].append(row)
            invalid_foreign_key_tables[row["parent"]][(branch, False)].append(row)
    for expr in (base_to_ours_parsed + base_to_theirs_parsed):
        table = get_target_table(expr)
        assert table is not None
        if table.name not in tables_set:
            tables_set.add(table.name)
            pragma_integrity_check_table_failed_and_recorded(
                invalid_tables, 
                table.name, 
                branch_and_cursor)
    for table_name in tables_set & invalid_foreign_key_tables.keys():
        for (branch, is_child_table), invalid_rows in invalid_foreign_key_tables[table_name].items():
            invalid_tables[table_name].invalid_reasons.append({
                "failed_branch": branch,
                "failure_type": f"pragma_foreign_check_failed - {'child table' if is_child_table else 'parent table'}",
                "failure_rows_or_details": invalid_rows
            })
    return tables_set, invalid_tables

def check_and_add_table_foreign_link_entry(
        table_to_table_foreign_link: dict[str, TableForeignLink], 
        child_table: str, 
        parent_table: str, 
        child_columns: tuple[str], 
        parent_columns: tuple[str]):
    if len(child_columns) == 0 or len(parent_columns) == 0:
        return
    table_to_table_foreign_link[child_table].parent_references[child_columns] = \
        ForeignLinkMetadata(parent_table, parent_columns)
    table_to_table_foreign_link[parent_table].child_references[parent_columns] = \
        ForeignLinkMetadata(child_table, child_columns)

def create_table_to_foreign_link_information(
        tables_set: set[str], 
        invalid_tables: dict[str, InvalidTableState],
        base_cursor: sqlite3.Cursor,
        ) -> dict[str, TableForeignLink]:
    table_to_table_foreign_link = defaultdict(lambda: TableForeignLink())
    for table in tables_set:
        if table in invalid_tables:
            continue
        rows = base_cursor.execute(f"SELECT * FROM pragma_foreign_key_list('{table}');").fetchall()
        if len(rows) == 0:
            continue
        curr_id = rows[0]["id"]
        curr_parent_table = rows[0]["table"]
        curr_child_columns = []
        curr_parent_columns = []
        for row in rows:
            if row["id"] != curr_id:
                check_and_add_table_foreign_link_entry(
                    table_to_table_foreign_link,
                    table, 
                    curr_parent_table, 
                    tuple(curr_child_columns), 
                    tuple(curr_parent_columns)
                    )
                curr_id = row["id"]
                curr_parent_table = row["table"]
                curr_child_columns = []
                curr_parent_columns = []
            if curr_parent_table not in tables_set or curr_parent_table in invalid_tables:
                continue
            curr_child_columns.append(row["from"])
            curr_parent_columns.append(row["to"])
        check_and_add_table_foreign_link_entry(
            table_to_table_foreign_link,
            table, 
            curr_parent_table, 
            tuple(curr_child_columns), 
            tuple(curr_parent_columns)
            )
    return table_to_table_foreign_link

def check_and_record_for_invalid_tables(invalid_tables, table_name, index, build_with_base_to_ours, is_building):
    '''returns true if table is invalid'''
    if table_name in invalid_tables:
        if build_with_base_to_ours and is_building or not build_with_base_to_ours and not is_building:
            invalid_tables[table_name].base_to_ours.append(index)
        else:
            invalid_tables[table_name].base_to_theirs.append(index)
        return True
    return False

def table_primary_keys(table_name: str, cursor: sqlite3.Cursor) -> dict[str, int]:
    rows = cursor.execute(f"SELECT pk, name FROM pragma_table_info('{table_name}') WHERE pk <> 0").fetchall()
    key_column_to_index: dict[str, int] = {row["name"]: row["pk"]-1 for row in rows}
    return key_column_to_index

def get_unique_indexes_column_names_and_data(table_name: str, cursor: sqlite3.Cursor
    )-> tuple[set[str], list[UniqueIndexState]]:
    rows = cursor.execute("SELECT il.name AS index_name, ii.name as column_name " 
                         f"FROM pragma_index_list('{table_name}') as il "
                         "JOIN pragma_index_info(il.name) AS ii "
                         "WHERE il.'unique' AND il.origin != 'pk'" \
                         "ORDER BY il.name").fetchall()
    if len(rows) == 0:
        return empty_set, empty_list
    columns_set: set[str] = {row["column_name"] for row in rows}
    index_to_cols = defaultdict(list)
    for row in rows:
        index_to_cols[row["index_name"]].append(row["column_name"])
    unique_indexes = [
        UniqueIndexState(index_name, tuple(cols))
        for index_name, cols in index_to_cols.items()
    ]
    return columns_set, unique_indexes

def get_primary_key_values_from_where(key_column_to_index: dict[str, int], expr: Expression) -> None | KeyTuple:
    primary_values: list[Expression | None] = [None for _ in range(len(key_column_to_index))]
    where = expr.args.get("where")
    if where is None:
        return None
    for predicate in where.find_all(expressions.EQ, expressions.Is):
        assert isinstance(predicate.this, expressions.Column)
        assert predicate.this.name in key_column_to_index
        if isinstance(predicate, expressions.Is):
            assert isinstance(predicate.expression, expressions.Null)
        else:
            assert isinstance(predicate.expression, expressions.Literal)
        primary_values[key_column_to_index[predicate.this.name]] = predicate.expression
    assert all(v is not None for v in primary_values)
    return tuple(primary_values) # type: ignore

def get_key_values_and_column_to_literal_insert(
        row: Expression, schema: Expression, key_column_to_index: dict[str, int]
        ) -> tuple[tuple[Expression, ...], dict[str, Expression]]:
    column_to_literal = {}
    primary_values = [None for _ in range(len(key_column_to_index))]
    for ident, lit in zip(schema.expressions, row.expressions):
        if ident.name in key_column_to_index:
            primary_values[key_column_to_index[ident.name]] = lit
        else:
            column_to_literal[ident.name] = lit
    assert all(v is not None for v in primary_values)
    return tuple(primary_values), column_to_literal # type: ignore

def add_or_check_unique_indexes_value(
    unique_indexes: list[UniqueIndexState],
    primary_key_columns_to_index: dict[str, int],
    i: int,
    table_name: str, 
    expr: Expression,
    cursor: sqlite3.Cursor,
    isAdd: bool,
    unique_index_columns_updated: set[str] = empty_set) -> None | list[tuple[int, str, tuple[str]]]:
    # only take insert or update type
    if len(unique_indexes) == 0:
        return
    if type(expr) is expressions.Update:
        where = expr.args.get("where")
        assert where is not None
    elif type(expr) is expressions.Insert:
        # Made use of assumption that sqldiff emits INSERT INTO table(column, ...) VALUES (...)
        # so sqlglot parses expr.this as Schema. If this stops being true, normalize
        # Insert target shape in one helper instead of duplicating logic here.
        assert isinstance(expr.this, expressions.Schema)
        assert expr.expression is not None
        # sqldiff emits one VALUES row per INSERT statement for row-level diffs.
        values_rows = expr.expression.expressions
        assert len(values_rows) == 1
        insert_row = values_rows[0]

        predicates: list[Expression] = []
        for ident, literal in zip(expr.this.expressions, insert_row.expressions):
            if ident.name not in primary_key_columns_to_index:
                continue
            column_expr = expressions.Column(this=expressions.Identifier(this=ident.name))
            if isinstance(literal, expressions.Null):
                predicates.append(expressions.Is(this=column_expr, expression=expressions.Null()))
            else:
                predicates.append(expressions.EQ(this=column_expr, expression=literal.copy()))

        assert len(predicates) > 0
        where_predicate = predicates[0]
        for predicate in predicates[1:]:
            where_predicate = expressions.And(this=where_predicate, expression=predicate)
        where = expressions.Where(this=where_predicate)
    else:
        return
    assert where is not None
    row = cursor.execute(f"SELECT * FROM {table_name} {where.sql()}").fetchone()
    conflicts = []
    for unique_index in unique_indexes:
        curr_unique_index_columns_updated = False
        has_null_value = False
        unique_index_columns_values = []
        for column_name in unique_index.column_names:
            curr_unique_index_columns_updated |= column_name in unique_index_columns_updated
            if row[column_name] is None:
                has_null_value = True
                break
            unique_index_columns_values.append(row[column_name])
        if not has_null_value and (curr_unique_index_columns_updated or type(expr) is expressions.Insert):
            tupled_unique_index_columns_values = tuple(unique_index_columns_values)
            if not isAdd and tupled_unique_index_columns_values in unique_index.values_to_stmt_index:
                conflicts.append(
                    (unique_index.values_to_stmt_index[tupled_unique_index_columns_values], 
                     unique_index.index_name, 
                     unique_index.column_names)
                    )
            unique_index.values_to_stmt_index[tupled_unique_index_columns_values] = i
    if conflicts:
        return conflicts
        

def add_to_res_match(res_match, build_with_base_to_ours, lookup_list_i, table_list_i):
    res_match.append(table_list_i if build_with_base_to_ours else lookup_list_i)

def get_ours_and_theirs_index_ordered(build_with_base_to_ours, lookup_list_i, table_list_i):
    return (table_list_i, lookup_list_i) if build_with_base_to_ours else (lookup_list_i, table_list_i)

def add_pk_conflict(conflict_stmts, build_with_base_to_ours, lookup_list_i, table_list_i):
    conflict_stmts[get_ours_and_theirs_index_ordered(
        build_with_base_to_ours, lookup_list_i, table_list_i
    )].append(conflict_pairs.PrimaryKeyConflict())

def check_conflict_and_return_final_diff(
        base_to_ours_parsed: list[Expression], 
        base_to_theirs_parsed: list[Expression],
        invalid_tables: dict[str, InvalidTableState],
        table_to_table_foreign_link: dict[str, TableForeignLink],
        base_cursor: sqlite3.Cursor,
        ours_cursor: sqlite3.Cursor,
        theirs_cursor: sqlite3.Cursor) -> DiffBuckets:
    '''approach:
    - build hash table keyed by primary key from base_to_ours_parsed
    - iterate through base_to_theirs_parsed for conflict / safe operations, raise
    exception and/or update the parsed list where applicable
    - return updated parsed list to apply / conflict if any
    Changes to data can be classified into insert, update and delete statements
    Let's consider the combinations of the statements with the same row (identified
    by primary key) in both base_to_ours and base_to_theirs 
    - insert-insert: check values the same
    - insert-update or insert-delete don't make sense so no need to consider
    - delete-delete: cross appliaction and diffing would handle this (no conflict)p
    - update-update: check values updated the same
    - update-delete: check primary key the same, and if YES raise conflict
    with sqldiff, the update and delete will always have its primary key value
    in the where clause
    '''
    build_cursor, lookup_cursor = ours_cursor, theirs_cursor
    table_name_to_table_conflict_state: dict[str, TableConflictState] = {}
    # build hashed table
    to_build, to_lookup = base_to_ours_parsed, base_to_theirs_parsed
    build_with_base_to_ours = True
    if len(base_to_theirs_parsed) < len(base_to_ours_parsed):
        to_build, to_lookup = to_lookup, to_build
        build_cursor, lookup_cursor = lookup_cursor, build_cursor
        build_with_base_to_ours = False
    for i, expr in enumerate(to_build):
        table = get_target_table(expr)
        assert table is not None
        if check_and_record_for_invalid_tables(invalid_tables, table.name, i, build_with_base_to_ours, is_building=True):
            continue
        if table.name not in table_name_to_table_conflict_state:
            table_name_to_table_conflict_state[table.name] = TableConflictState(
                # assume no change in primary key / unique indexes between base ours and theirs
                table_primary_keys(table.name, base_cursor),
                {},
                *get_unique_indexes_column_names_and_data(table.name, base_cursor)
            )
        table_conflict_state: TableConflictState = table_name_to_table_conflict_state[table.name]
        key_values_to_data = table_conflict_state.primary_key_value_to_data
        key_values = get_primary_key_values_from_where(
            table_conflict_state.primary_key_columns_to_index,
            expr
        )
        if isinstance(expr, expressions.Delete):
            assert key_values is not None
            key_values_to_data[key_values] = (i, expressions.Delete, None)
        elif isinstance(expr, expressions.Insert):
            for row in expr.expression.expressions:
                primary_values_tuple, column_to_literal = \
                    get_key_values_and_column_to_literal_insert(row, expr.this, table_conflict_state.primary_key_columns_to_index)
                key_values_to_data[tuple(primary_values_tuple)] = (i, expressions.Insert, column_to_literal) # type: ignore
            add_or_check_unique_indexes_value(
                table_conflict_state.unique_indexes,
                table_conflict_state.primary_key_columns_to_index,
                i, 
                table.name, 
                expr, 
                build_cursor,
                True)
        elif isinstance(expr, expressions.Update):
            column_to_literal = {}
            assert key_values is not None
            are_unique_index_column_updated = False
            unique_index_columns_updated = set()
            for e in expr.args["expressions"]:
                column_to_literal[e.this.name] = e.expression
                are_unique_index_column_updated |= e.this.name in table_conflict_state.unique_key_column_names
                unique_index_columns_updated.add(e.this.name)
            key_values_to_data[key_values] = (i, expressions.Update, column_to_literal)
            if are_unique_index_column_updated:
                add_or_check_unique_indexes_value(
                    table_conflict_state.unique_indexes,
                    table_conflict_state.primary_key_columns_to_index,
                    i, 
                    table.name, 
                    expr, 
                    build_cursor,
                    True,
                    unique_index_columns_updated)
    res_match = [] # using index from base_to_ours
    extra_stmts_base_to_ours = []
    extra_stmts_base_to_theirs = []
    conflict_stmts_pair = defaultdict(list) # key:(base_to_ours index, base_to_theirs index)
    list_to_add_for_failed_lookup = extra_stmts_base_to_theirs \
        if build_with_base_to_ours \
        else extra_stmts_base_to_ours
    for i, expr in enumerate(to_lookup):
        table = get_target_table(expr)
        assert table is not None
        if check_and_record_for_invalid_tables(invalid_tables, table.name, i, build_with_base_to_ours, is_building=False):
            continue
        if table.name not in table_name_to_table_conflict_state:
            list_to_add_for_failed_lookup.append(i)
            continue
        table_conflict_state: TableConflictState = table_name_to_table_conflict_state[table.name]
        key_values_to_data = table_conflict_state.primary_key_value_to_data
        key_values = get_primary_key_values_from_where(
            table_conflict_state.primary_key_columns_to_index,
            expr
        )
        if isinstance(expr, expressions.Delete):
            assert key_values is not None
            if key_values not in key_values_to_data:
                list_to_add_for_failed_lookup.append(i)
            else:
                i2, t, column_to_literal = key_values_to_data[key_values]
                if t is expressions.Delete:
                    add_to_res_match(res_match, build_with_base_to_ours, i, i2)
                elif t is expressions.Update:
                    add_pk_conflict(conflict_stmts_pair, build_with_base_to_ours, i, i2)
                key_values_to_data.pop(key_values)

        elif isinstance(expr, expressions.Insert):
            check_entry = add_or_check_unique_indexes_value(
                table_conflict_state.unique_indexes,
                table_conflict_state.primary_key_columns_to_index,
                i, 
                table.name, 
                expr, 
                lookup_cursor,
                False)
            if check_entry is not None:
                for entry in check_entry:
                    conflict_stmts_pair[get_ours_and_theirs_index_ordered(
                        build_with_base_to_ours, i, entry[0]
                    )].append(conflict_pairs.UniqueIndexesConflict(
                        entry[1], entry[2]))
            for row in expr.expression.expressions:
                primary_values_tuple, curr_column_to_literal = \
                    get_key_values_and_column_to_literal_insert(row, expr.this, table_conflict_state.primary_key_columns_to_index)
                if primary_values_tuple not in key_values_to_data:
                    if check_entry is None:
                        list_to_add_for_failed_lookup.append(i)
                else:
                    i2, t, column_to_literal = key_values_to_data[primary_values_tuple]
                    assert t is expressions.Insert
                    if column_to_literal == curr_column_to_literal:
                        if check_entry is None:
                            add_to_res_match(res_match, build_with_base_to_ours, i, i2)
                    else:
                        add_pk_conflict(conflict_stmts_pair, build_with_base_to_ours, i, i2)
                    key_values_to_data.pop(primary_values_tuple)
        elif isinstance(expr, expressions.Update):
            assert key_values is not None
            curr_column_to_literal = {}
            are_unique_index_column_updated = False
            check_entry = None
            unique_index_columns_updated = set()
            for e in expr.args["expressions"]:
                curr_column_to_literal[e.this.name] = e.expression
                are_unique_index_column_updated |= e.this.name in table_conflict_state.unique_key_column_names
                unique_index_columns_updated.add(e.this.name)
            if are_unique_index_column_updated:
                check_entry = add_or_check_unique_indexes_value(
                    table_conflict_state.unique_indexes,
                    table_conflict_state.primary_key_columns_to_index,
                    i, 
                    table.name, 
                    expr, 
                    lookup_cursor,
                    False,
                    unique_index_columns_updated)
                if check_entry is not None:
                    for entry in check_entry:
                        conflict_stmts_pair[get_ours_and_theirs_index_ordered(
                            build_with_base_to_ours, i, entry[0]
                        )].append(conflict_pairs.UniqueIndexesConflict(
                            entry[1], entry[2]))
            if key_values not in key_values_to_data:
                if check_entry is None:
                    list_to_add_for_failed_lookup.append(i)
            else:
                i2, t, column_to_literal = key_values_to_data[key_values]
                if t is expressions.Delete:
                    add_pk_conflict(conflict_stmts_pair, build_with_base_to_ours, i, i2)
                elif t is expressions.Update:
                    if curr_column_to_literal == column_to_literal:
                        if check_entry is None:
                            add_to_res_match(res_match, build_with_base_to_ours, i, i2)
                    else:
                        add_pk_conflict(conflict_stmts_pair, build_with_base_to_ours, i, i2)
                key_values_to_data.pop(key_values)

    list_to_add_for_remaining_table_entries = extra_stmts_base_to_ours \
        if build_with_base_to_ours \
        else extra_stmts_base_to_theirs
    for table_conflict_state in table_name_to_table_conflict_state.values():
        list_to_add_for_remaining_table_entries.extend(
            data[0] for data in table_conflict_state.primary_key_value_to_data.values())
    return DiffBuckets(res_match, extra_stmts_base_to_ours, extra_stmts_base_to_theirs, conflict_stmts_pair)

def main():
    parser = argparse.ArgumentParser(description='SQLite merge driver')
    parser.add_argument('base', metavar='%O', help='Base file')
    parser.add_argument('ours', metavar='%A', help='Ours file')
    parser.add_argument('theirs', metavar='%B', help='Theirs file')
    # irrelevant for our case
    parser.add_argument('conflict_marker_size', metavar='%L', type=int, help='Conflict marker size')
    # %P - path of file as it appears in repo - potentially useful for debugging or logging, ignore for now
    parser.add_argument('pathname', metavar='%P', help='Pathname of file being merged')
    args = parser.parse_args()
    # res1/2: start and end of list are begin and commit of transaction, the rest being the sql statements
    base_to_ours = utils.subprocess_run_wrapper(
        ["sqldiff", "--primarykey", args.base, args.ours], text=True, capture_output=True
        ).stdout.strip().split('\n')
    base_to_theirs = utils.subprocess_run_wrapper(
        ["sqldiff", "--primarykey", args.base, args.theirs], text=True, capture_output=True
        ).stdout.strip().split('\n')
    base_to_ours_parsed: list[Expression] = [sqlglot.parse_one(statement) for statement in base_to_ours]
    base_to_theirs_parsed: list[Expression] = [sqlglot.parse_one(statement) for statement in base_to_theirs]

    with closing(sqlite3.connect(args.base)) as base_con, \
     closing(sqlite3.connect(args.ours)) as ours_con, \
     closing(sqlite3.connect(args.theirs)) as theirs_con:
        base_con.row_factory = sqlite3.Row
        ours_con.row_factory = sqlite3.Row
        theirs_con.row_factory = sqlite3.Row
        base_cursor = base_con.cursor()
        ours_cursor = ours_con.cursor()
        theirs_cursor = theirs_con.cursor()

        tables_set, invalid_tables = check_valid_tables(base_to_ours_parsed, base_to_theirs_parsed, base_cursor, ours_cursor, theirs_cursor)
        table_to_table_foreign_link = create_table_to_foreign_link_information(tables_set, invalid_tables, base_cursor)
        diffs = check_conflict_and_return_final_diff(base_to_ours_parsed, base_to_theirs_parsed, invalid_tables, table_to_table_foreign_link, base_cursor, ours_cursor, theirs_cursor)

    stmts_to_apply = \
        [base_to_ours[i] for i in diffs.matched_ours_indices] + \
        [base_to_ours[i] for i in diffs.extra_ours_indices] + \
        [base_to_theirs[i] for i in diffs.extra_theirs_indices]
    shutil.copy2(args.base, args.ours)
    utils.subprocess_run_wrapper(
        ['sqlite3', args.ours], 
        input='\n'.join(stmts_to_apply), 
        text=True, 
        capture_output=True)
    if len(diffs.conflict_pairs) > 0 or len(invalid_tables) > 0:
        out = {
            "conflicts": [{
                "conflict_stmts": (base_to_ours[i1], base_to_theirs[i2]),
                "conflict_details": [conflict_pair.to_dict() for conflict_pair in conflict_pairs]
                } 
                for (i1, i2), conflict_pairs in diffs.conflict_pairs.items()],
            "invalid_tables": {table_name: asdict(state) for table_name, state in invalid_tables.items()}
        }
        conflict_file = f"{args.pathname}-.merge_file.json"
        with open(conflict_file, "w") as f:
            json.dump(out, f, indent=2)
        sys.exit(1)
    sys.exit(0)

if __name__ == '__main__':
    main()